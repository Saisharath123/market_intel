"""Regression checks for version-compatible, scoped Sonar initialization."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def setup_module():
    path = Path(__file__).resolve().parents[1] / 'infra' / 'sonarqube-configure.py'
    spec = importlib.util.spec_from_file_location('sonarqube_setup', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('default_name', ['sonar-users', 'authenticated-users'])
def test_remove_only_present_default_grants_with_anyone_absent(setup_module, monkeypatch, default_name):
    """A missing legacy Anyone group must never produce a remove request."""
    removals = []

    def api(path, *_args, fields=None):
        if path.startswith('user_groups/search'):
            return {'groups': [{'name': default_name, 'default': True}]}
        if path.startswith('permissions/groups?permission=scan'):
            return {'groups': [{'name': default_name}, {'name': 'sonar-administrators'}]}
        if path.startswith('permissions/groups?permission=provisioning'):
            return {'groups': [{'name': 'sonar-administrators'}]}
        assert path == 'permissions/remove_group'
        removals.append(fields)
        return {}

    monkeypatch.setattr(setup_module, 'api', api)
    setup_module.restrict_default_permissions('unused-test-credential')
    assert removals == [{'groupName': default_name, 'permission': 'scan'}]


def test_already_clean_permissions_are_idempotent(setup_module, monkeypatch):
    def api(path, *_args, fields=None):
        if path.startswith('user_groups/search'):
            return {'groups': [{'name': 'sonar-users', 'default': True}]}
        assert path.startswith('permissions/groups?permission=')
        return {'groups': [{'name': 'sonar-administrators'}]}

    monkeypatch.setattr(setup_module, 'api', api)
    setup_module.restrict_default_permissions('unused-test-credential')


def test_legacy_anyone_grants_are_removed_only_when_present(setup_module, monkeypatch):
    removals = []

    def api(path, *_args, fields=None):
        if path.startswith('user_groups/search'):
            return {'groups': [{'name': 'sonar-users', 'default': True}]}
        if path.startswith('permissions/groups?permission=scan'):
            return {'groups': [{'name': 'Anyone'}]}
        if path.startswith('permissions/groups?permission=provisioning'):
            return {'groups': []}
        assert path == 'permissions/remove_group'
        removals.append(fields)
        return {}

    monkeypatch.setattr(setup_module, 'api', api)
    setup_module.restrict_default_permissions('unused-test-credential')
    assert removals == [{'groupName': 'Anyone', 'permission': 'scan'}]


@pytest.mark.parametrize('result', [
    {'groups': []},
    {'groups': [{'name': 'one', 'default': True}, {'name': 'two', 'default': True}]},
    {'paging': {'total': 101}, 'groups': [{'name': 'sonar-users', 'default': True}]},
])
def test_incomplete_or_ambiguous_group_lookup_stops_before_changes(setup_module, monkeypatch, result):
    def api(path, *_args, fields=None):
        assert path.startswith('user_groups/search')
        return result

    monkeypatch.setattr(setup_module, 'api', api)
    with pytest.raises(RuntimeError):
        setup_module.restrict_default_permissions('unused-test-credential')
