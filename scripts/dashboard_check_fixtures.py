from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTION = ROOT / 'actions' / 'dashboard-check'
FIXTURE = ROOT / 'scripts' / 'testdata' / 'dashboard-check' / 'valid.json'

_spec = importlib.util.spec_from_file_location('dashboard_check', ACTION / 'dashboard-check.py')
dc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dc
_spec.loader.exec_module(dc)

CLASSIC = {'uid': 'job-queue', 'title': 'Job queue', 'panels': [], 'schemaVersion': 42}
V2_STUB = {
    'apiVersion': 'dashboard.grafana.app/v2',
    'kind': 'Dashboard',
    'metadata': {'name': 'app'},
    'spec': {},
}


def valid() -> dict:
    return json.loads(FIXTURE.read_text())


def element(doc: dict, name: str) -> dict:
    return doc['spec']['elements'][name]['spec']


def defaults(doc: dict, name: str) -> dict:
    return element(doc, name)['vizConfig']['spec']['fieldConfig']['defaults']


def rows(doc: dict) -> list:
    return doc['spec']['layout']['spec']['rows']


def tabs_of(*tabs: tuple[str, dict]) -> dict:
    return {
        'kind': 'TabsLayout',
        'spec': {
            'tabs': [
                {'kind': 'TabsLayoutTab', 'spec': {'title': title, 'layout': layout}}
                for title, layout in tabs
            ]
        },
    }


def grid_items(doc: dict) -> list:
    return rows(doc)[0]['spec']['layout']['spec']['items']


def variable(doc: dict, name: str) -> dict:
    return next(v for v in doc['spec']['variables'] if v['spec']['name'] == name)


def conditional_group() -> dict:
    return {
        'kind': 'ConditionalRenderingGroup',
        'spec': {'visibility': 'show', 'condition': 'and', 'items': []},
    }


STRAY_CONDITIONAL_PLACES = {
    'panel spec': lambda doc: element(doc, 'panel-1'),
    'layout spec': lambda doc: rows(doc)[0]['spec']['layout']['spec'],
    'dashboard spec': lambda doc: doc['spec'],
}


def run_cli(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = dc.main(list(args))
    return code, out.getvalue()
