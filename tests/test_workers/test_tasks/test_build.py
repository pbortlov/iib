# SPDX-License-Identifier: GPL-3.0-or-later
import os
import re
import textwrap
from unittest import mock

import pytest

from iib.exceptions import IIBError
from iib.workers.tasks import build


@mock.patch('iib.workers.tasks.build.run_cmd')
def test_build_image(mock_run_cmd):
    build._build_image('/some/dir', 3)

    mock_run_cmd.assert_called_once()
    build_args = mock_run_cmd.call_args[0][0]
    assert build_args[0:2] == ['podman', 'build']
    assert '/some/dir/index.Dockerfile' in build_args


@mock.patch('iib.workers.tasks.build.run_cmd')
def test_cleanup(mock_run_cmd):
    build._cleanup()

    mock_run_cmd.assert_called_once()
    rmi_args = mock_run_cmd.call_args[0][0]
    assert rmi_args[0:2] == ['podman', 'rmi']


@mock.patch('iib.workers.tasks.build.tempfile.TemporaryDirectory')
@mock.patch('iib.workers.tasks.build.run_cmd')
def test_create_and_push_manifest_list(mock_run_cmd, mock_td, tmp_path):
    mock_td.return_value.__enter__.return_value = tmp_path

    build._create_and_push_manifest_list(3, {'amd64', 's390x'})

    expected_manifest = textwrap.dedent(
        '''\
        image: registry:8443/operator-registry-index:3
        manifests:
        - image: registry:8443/operator-registry-index:3-amd64
          platform:
            architecture: amd64
            os: linux
        - image: registry:8443/operator-registry-index:3-s390x
          platform:
            architecture: s390x
            os: linux
        '''
    )
    manifest = os.path.join(tmp_path, 'manifest.yaml')
    with open(manifest, 'r') as manifest_f:
        assert manifest_f.read() == expected_manifest
    mock_run_cmd.assert_called_once()
    manifest_tool_args = mock_run_cmd.call_args[0][0]
    assert manifest_tool_args[0] == 'manifest-tool'
    assert manifest in manifest_tool_args


@mock.patch('iib.workers.tasks.build.update_request')
def test_finish_request_post_build(mock_ur):
    output_pull_spec = 'quay.io/namespace/some-image:3'
    request_id = 2
    arches = {'amd64'}
    build._finish_request_post_build(output_pull_spec, request_id, arches)

    mock_ur.assert_called_once()
    update_request_payload = mock_ur.call_args[0][1]
    assert update_request_payload.keys() == {'index_image', 'state', 'state_reason'}
    assert update_request_payload['index_image'] == output_pull_spec


def test_fix_opm_path(tmpdir):
    dockerfile = tmpdir.join('index.Dockerfile')
    dockerfile.write('FROM image as builder\nFROM scratch\nCOPY --from=builder /build/bin/opm /opm')

    build._fix_opm_path(str(tmpdir))

    assert dockerfile.read() == (
        'FROM image as builder\nFROM scratch\nCOPY --from=builder /bin/opm /opm'
    )


@pytest.mark.parametrize('request_id', (1, 5))
def test_get_local_pull_spec(request_id):
    rv = build._get_local_pull_spec(request_id)

    assert re.match(f'.+:{request_id}', rv)


@mock.patch('iib.workers.tasks.build.skopeo_inspect')
def test_get_image_arches(mock_si):
    mock_si.return_value = {
        'mediaType': 'application/vnd.docker.distribution.manifest.list.v2+json',
        'manifests': [
            {'platform': {'architecture': 'amd64'}},
            {'platform': {'architecture': 's390x'}},
        ],
    }
    rv = build._get_image_arches('image:latest')
    assert rv == {'amd64', 's390x'}


@mock.patch('iib.workers.tasks.build.skopeo_inspect')
def test_get_image_arches_manifest(mock_si):
    mock_si.side_effect = [
        {'mediaType': 'application/vnd.docker.distribution.manifest.v2+json'},
        {'Architecture': 'amd64'},
    ]
    rv = build._get_image_arches('image:latest')
    assert rv == {'amd64'}


@mock.patch('iib.workers.tasks.build.skopeo_inspect')
def test_get_image_arches_not_manifest_list(mock_si):
    mock_si.return_value = {'mediaType': 'application/vnd.docker.distribution.notmanifest.v2+json'}
    with pytest.raises(IIBError, match='.+is neither a v2 manifest list nor a v2 manifest'):
        build._get_image_arches('image:latest')


@pytest.mark.parametrize('label, expected', (('some_label', 'value'), ('not_there', None)))
@mock.patch('iib.workers.tasks.utils.skopeo_inspect')
def test_get_image_label(mock_si, label, expected):
    mock_si.return_value = {'Labels': {'some_label': 'value'}}
    assert build.get_image_label('some-image:latest', label) == expected


@mock.patch('iib.workers.tasks.build.skopeo_inspect')
def test_get_resolved_image(mock_si):
    mock_si.return_value = {'Digest': 'sha256:abcdefg', 'Name': 'some-image'}
    rv = build._get_resolved_image('some-image')
    assert rv == 'some-image@sha256:abcdefg'


@mock.patch('iib.workers.tasks.build.time.sleep')
@mock.patch('iib.workers.tasks.build.get_request')
def test_poll_request(mock_gr, mock_sleep):
    mock_gr.side_effect = [
        {'arches': [], 'state': 'in_progress'},
        {'arches': ['amd64'], 'state': 'in_progress'},
        {'arches': ['s390x'], 'state': 'in_progress'},
    ]

    assert build._poll_request(3, {'amd64', 's390x'}) is True
    mock_sleep.call_count == 3
    mock_gr.call_count == 3


@mock.patch('iib.workers.tasks.build.time.sleep')
@mock.patch('iib.workers.tasks.build.get_request')
def test_poll_request_request_failed(mock_gr, mock_sleep):
    mock_gr.side_effect = [
        {'arches': [], 'state': 'in_progress'},
        {'arches': [], 'state': 'failed'},
    ]

    assert build._poll_request(3, {'amd64', 's390x'}) is False
    mock_sleep.call_count == 2
    mock_gr.call_count == 2


@pytest.mark.parametrize(
    'add_arches, from_index, from_index_arches, bundles, expected_bundle_mapping',
    (
        ([], 'some-index:latest', {'amd64'}, None, {}),
        (['amd64', 's390x'], None, set(), None, {}),
        (['s390x'], 'some-index:latest', {'amd64'}, None, {}),
        (
            ['amd64'],
            None,
            set(),
            ['quay.io/some-bundle:v1', 'quay.io/some-bundle2:v1'],
            {
                'some-bundle': ['quay.io/some-bundle:v1'],
                'some-bundle2': ['quay.io/some-bundle2:v1'],
            },
        ),
    ),
)
@mock.patch('iib.workers.tasks.build.set_request_state')
@mock.patch('iib.workers.tasks.build._get_resolved_image')
@mock.patch('iib.workers.tasks.build._get_image_arches')
@mock.patch('iib.workers.tasks.build.get_image_label')
@mock.patch('iib.workers.tasks.build.update_request')
def test_prepare_request_for_build(
    mock_ur,
    mock_gil,
    mock_gia,
    mock_gri,
    mock_srs,
    add_arches,
    from_index,
    from_index_arches,
    bundles,
    expected_bundle_mapping,
):
    binary_image_resolved = 'binary-image@sha256:abcdef'
    from_index_resolved = None
    expected_arches = set(add_arches) | from_index_arches
    expected_payload_keys = {'binary_image_resolved', 'bundle_mapping', 'state', 'state_reason'}
    if from_index:
        from_index_name = from_index.split(':', 1)[0]
        from_index_resolved = f'{from_index_name}@sha256:bcdefg'
        mock_gri.side_effect = [binary_image_resolved, from_index_resolved]
        mock_gia.side_effect = [expected_arches, from_index_arches]
        expected_payload_keys.add('from_index_resolved')
    else:
        mock_gri.side_effect = [binary_image_resolved]
        mock_gia.side_effect = [expected_arches]

    if bundles:
        mock_gil.side_effect = [bundle.rsplit('/', 1)[1].split(':', 1)[0] for bundle in bundles]

    rv = build._prepare_request_for_build('binary-image:latest', 1, from_index, add_arches, bundles)
    assert rv == {
        'arches': expected_arches,
        'binary_image_resolved': binary_image_resolved,
        'from_index_resolved': from_index_resolved,
    }
    mock_ur.assert_called_once()
    update_request_payload = mock_ur.call_args[0][1]
    assert update_request_payload['bundle_mapping'] == expected_bundle_mapping
    assert update_request_payload.keys() == expected_payload_keys


@mock.patch('iib.workers.tasks.build.set_request_state')
@mock.patch('iib.workers.tasks.build._get_resolved_image')
@mock.patch('iib.workers.tasks.build._get_image_arches')
def test_prepare_request_for_build_no_arches(mock_gia, mock_gri, mock_srs):
    mock_gia.side_effect = [{'amd64'}]

    with pytest.raises(IIBError, match='No arches.+'):
        build._prepare_request_for_build('binary-image:latest', 1)


@mock.patch('iib.workers.tasks.build.set_request_state')
@mock.patch('iib.workers.tasks.build._get_resolved_image')
@mock.patch('iib.workers.tasks.build._get_image_arches')
def test_prepare_request_for_build_no_arch_worker(mock_gia, mock_gri, mock_srs):
    mock_gia.side_effect = [{'amd64', 'arm64'}]

    expected = 'Building for the following requested arches is not supported.+'
    with pytest.raises(IIBError, match=expected):
        build._prepare_request_for_build('binary-image:latest', 1, add_arches=['arm64'])


@mock.patch('iib.workers.tasks.build.set_request_state')
@mock.patch('iib.workers.tasks.build._get_resolved_image')
@mock.patch('iib.workers.tasks.build._get_image_arches')
def test_prepare_request_for_build_binary_image_no_arch(mock_gia, mock_gri, mock_srs):
    mock_gia.side_effect = [{'amd64'}]

    expected = 'The binary image is not available for the following arches.+'
    with pytest.raises(IIBError, match=expected):
        build._prepare_request_for_build('binary-image:latest', 1, add_arches=['s390x'])


@mock.patch('iib.workers.tasks.build._get_local_pull_spec')
@mock.patch('iib.workers.tasks.build.run_cmd')
def test_push_arch_image(mock_run_cmd, mock_glps):
    mock_glps.return_value = 'source:tag'

    build._push_arch_image(3)

    mock_run_cmd.assert_called_once()
    push_args = mock_run_cmd.call_args[0][0]
    assert push_args[0:2] == ['podman', 'push']
    assert 'source:tag' in push_args
    assert 'docker://registry:8443/operator-registry-index:3-amd64' in push_args


@pytest.mark.parametrize('request_succeeded', (True, False))
@mock.patch('iib.workers.tasks.build._verify_labels')
@mock.patch('iib.workers.tasks.build._prepare_request_for_build')
@mock.patch('iib.workers.tasks.build.opm_index_add')
@mock.patch('iib.workers.tasks.build._poll_request')
@mock.patch('iib.workers.tasks.build._verify_index_image')
@mock.patch('iib.workers.tasks.build._finish_request_post_build')
@mock.patch('iib.workers.tasks.build.opm_index_export')
@mock.patch('iib.workers.tasks.build.set_request_state')
@mock.patch('iib.workers.tasks.build._create_and_push_manifest_list')
@mock.patch('iib.workers.tasks.build.get_legacy_support_packages')
@mock.patch('iib.workers.tasks.build.validate_legacy_params_and_config')
@mock.patch('iib.workers.tasks.build._cleanup')
def test_handle_add_request(
    mock_cleanup,
    mock_vlpc,
    mock_glsp,
    mock_capml,
    mock_srs,
    mock_oie,
    mock_frpb,
    mock_vii,
    mock_pr,
    mock_oia,
    mock_prfb,
    mock_vl,
    request_succeeded,
):
    arches = {'amd64', 's390x'}
    mock_prfb.return_value = {
        'arches': arches,
        'binary_image_resolved': 'binary-image@sha256:abcdef',
        'from_index_resolved': 'from-index@sha256:bcdefg',
    }
    mock_glsp.return_value = {'some_package'}
    mock_pr.return_value = request_succeeded
    output_pull_spec = 'quay.io/namespace/some-image:3'
    mock_capml.return_value = output_pull_spec
    build.handle_add_request(
        ['some-bundle:2.3-1'],
        'binary-image:latest',
        3,
        'from-index:latest',
        ['s390x'],
        'token',
        'org',
    )
    mock_vl.assert_called_once()
    mock_prfb.assert_called_once()
    mock_oia.apply_async.call_count == 2
    # Verify opm_index_add was scheduled on the correct workers
    for i, arch in enumerate(sorted(arches)):
        assert mock_oia.apply_async.call_args_list[i][1]['queue'] == f'iib_{arch}'
        assert mock_oia.apply_async.call_args_list[i][1]['routing_key'] == f'iib_{arch}'
    mock_pr.assert_called_once()
    if request_succeeded:
        mock_oie.assert_called_once()
        mock_frpb.assert_called_once()
        mock_vii.assert_called_once()
        mock_capml.assert_called_once()
        mock_srs.assert_called_once()
        mock_cleanup.assert_called_once()
    else:
        mock_oie.assert_not_called()
        mock_frpb.assert_not_called()
        mock_vii.assert_not_called()
        mock_capml.assert_not_called()
        mock_srs.assert_not_called()
        mock_cleanup.assert_not_called()


@pytest.mark.parametrize('from_index', (None, 'some_index:latest'))
@mock.patch('iib.workers.tasks.build.get_request')
@mock.patch('iib.workers.tasks.build._cleanup')
@mock.patch('iib.workers.tasks.build._fix_opm_path')
@mock.patch('iib.workers.tasks.build._build_image')
@mock.patch('iib.workers.tasks.build._push_arch_image')
@mock.patch('iib.workers.tasks.build.run_cmd')
@mock.patch('iib.workers.tasks.build.update_request')
def test_opm_index_add(
    mock_ur, mock_run_cmd, mock_pai, mock_bi, mock_fop, mock_cleanup, mock_gr, from_index
):
    mock_gr.return_value = {'state': 'in_progress'}
    binary_images = ['bundle:1.2', 'bundle:1.3']
    build.opm_index_add(binary_images, 'binary-image:latest', 3, from_index=from_index)

    # This is only directly called once in the actual function
    mock_run_cmd.assert_called_once()
    opm_args = mock_run_cmd.call_args[0][0]
    assert opm_args[0:3] == ['opm', 'index', 'add']
    assert ','.join(binary_images) in opm_args
    if from_index:
        assert '--from-index' in opm_args
        assert from_index in opm_args
    else:
        assert '--from-index' not in opm_args
    mock_gr.assert_called_once_with(3)
    mock_cleanup.assert_called_once()
    mock_fop.assert_called_once()
    mock_bi.assert_called_once()
    mock_pai.assert_called_once()
    mock_ur.assert_called_once()


@mock.patch('iib.workers.tasks.build.get_request')
@mock.patch('iib.workers.tasks.build.set_request_state')
@mock.patch('iib.workers.tasks.build._build_image')
def test_opm_index_add_already_failed(mock_bi, mock_srs, mock_gr):
    mock_gr.return_value = {'state': 'failed'}
    binary_images = ['bundle:1.2', 'bundle:1.3']
    build.opm_index_add(binary_images, 'binary-image:latest', 3)

    mock_srs.assert_called_once()
    mock_gr.assert_called_once_with(3)
    mock_bi.assert_not_called()


@pytest.mark.parametrize('request_succeeded', (True, False))
@mock.patch('iib.workers.tasks.build._prepare_request_for_build')
@mock.patch('iib.workers.tasks.build.opm_index_rm')
@mock.patch('iib.workers.tasks.build._poll_request')
@mock.patch('iib.workers.tasks.build._verify_index_image')
@mock.patch('iib.workers.tasks.build._finish_request_post_build')
def test_handle_rm_request(
    mock_frpb, mock_vii, mock_pr, mock_oir, mock_prfb, request_succeeded,
):
    arches = {'amd64', 's390x'}
    mock_prfb.return_value = {
        'arches': arches,
        'binary_image_resolved': 'binary-image@sha256:abcdef',
        'from_index_resolved': 'from-index@sha256:bcdefg',
    }
    mock_pr.return_value = request_succeeded
    build.handle_rm_request(['some-operator'], 'binary-image:latest', 3, 'from-index:latest')

    mock_prfb.assert_called_once()
    mock_oir.apply_async.call_count == 2
    # Verify opm_index_add was scheduled on the correct workers
    for i, arch in enumerate(sorted(arches)):
        assert mock_oir.apply_async.call_args_list[i][1]['queue'] == f'iib_{arch}'
        assert mock_oir.apply_async.call_args_list[i][1]['routing_key'] == f'iib_{arch}'
    mock_pr.assert_called_once()
    if request_succeeded:
        mock_vii.assert_called_once()
        mock_frpb.assert_called_once()
    else:
        mock_vii.assert_not_called()
        mock_frpb.assert_not_called()


@mock.patch('iib.workers.tasks.build.get_request')
@mock.patch('iib.workers.tasks.build._cleanup')
@mock.patch('iib.workers.tasks.build._fix_opm_path')
@mock.patch('iib.workers.tasks.build._build_image')
@mock.patch('iib.workers.tasks.build._push_arch_image')
@mock.patch('iib.workers.tasks.build.run_cmd')
@mock.patch('iib.workers.tasks.build.update_request')
def test_opm_index_rm(mock_ur, mock_run_cmd, mock_pai, mock_bi, mock_fop, mock_cleanup, mock_gr):
    mock_gr.return_value = {'state': 'in_progress'}
    operators = ['operator_1', 'operator_2']
    build.opm_index_rm(operators, 'binary-image:latest', 3, 'some_index:latest')

    # This is only directly called once in the actual function
    mock_run_cmd.assert_called_once()
    opm_args = mock_run_cmd.call_args[0][0]
    assert opm_args[0:3] == ['opm', 'index', 'rm']
    assert ','.join(operators) in opm_args
    assert 'some_index:latest' in opm_args
    mock_gr.assert_called_once_with(3)
    mock_cleanup.assert_called_once()
    mock_fop.assert_called_once()
    mock_bi.assert_called_once()
    mock_pai.assert_called_once()
    mock_ur.assert_called_once()


@mock.patch('iib.workers.tasks.build.get_request')
@mock.patch('iib.workers.tasks.build.set_request_state')
@mock.patch('iib.workers.tasks.build._build_image')
def test_opm_index_rm_already_failed(mock_bi, mock_srs, mock_gr):
    mock_gr.return_value = {'state': 'failed'}
    operators = ['operator_1', 'operator_2']
    build.opm_index_rm(operators, 'binary-image:latest', 3, 'from:index')

    mock_srs.assert_called_once()
    mock_gr.assert_called_once_with(3)
    mock_bi.assert_not_called()


@mock.patch('iib.workers.tasks.build._get_resolved_image')
def test_verify_index_image_failure(mock_ri):
    mock_ri.return_value = 'image:works'
    match_str = (
        'The supplied from_index image changed during the IIB request.'
        ' Please resubmit the request.'
    )
    with pytest.raises(IIBError, match=match_str):
        build._verify_index_image('image:doesnt_work', 'unresolved_image')


@pytest.mark.parametrize(
    'iib_required_labels', ({'com.redhat.delivery.operator.bundle': 'true'}, {})
)
@mock.patch('iib.workers.tasks.build.get_worker_config')
@mock.patch('iib.workers.tasks.build.get_image_labels')
def test_verify_labels(mock_gil, mock_gwc, iib_required_labels):
    mock_gwc.return_value = {'iib_required_labels': iib_required_labels}
    mock_gil.return_value = {'com.redhat.delivery.operator.bundle': 'true'}
    build._verify_labels(['some-bundle:v1.0'])

    if iib_required_labels:
        mock_gil.assert_called_once()
    else:
        mock_gil.assert_not_called()


@mock.patch('iib.workers.tasks.build.get_worker_config')
@mock.patch('iib.workers.tasks.build.get_image_labels')
def test_verify_labels_fails(mock_gil, mock_gwc):
    mock_gwc.return_value = {'iib_required_labels': {'com.redhat.delivery.operator.bundle': 'true'}}
    mock_gil.return_value = {'lunch': 'pizza'}
    with pytest.raises(IIBError, match='som'):
        build._verify_labels(['some-bundle:v1.0'])
