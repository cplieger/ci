#!/usr/bin/env python3
"""Check Grafana schema v2 dashboard files against the dashboard standard.

A classic file (a string uid, no apiVersion) passes with a notice unless the
base copy is schema v2. The base copy pins metadata.name, and a base copy of
neither shape fails every FILE. Exit 0 on pass, 1 on a finding, 2 on usage.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

API_VERSION = 'dashboard.grafana.app/v2'
ENVELOPE_KEYS = ('apiVersion', 'kind', 'metadata', 'spec')

LAYOUT_KINDS = {'GridLayout', 'AutoGridLayout', 'RowsLayout', 'TabsLayout'}
NO_VALUE_TYPES = {'stat', 'gauge', 'bargauge', 'table'}
CLOSED_SET_VARIABLES = {'QueryVariable', 'CustomVariable'}
UNLABELLED_HIDE = {'hideVariable', 'hideLabel'}
DATASOURCE_TAGS = {'loki', 'prometheus', 'mimir'}
VARIABLE_REF = re.compile(r'^\$\{\w+\}$')
TABLE_CELL_OVERRIDE = {
    'matcher': {'id': 'byRegexp', 'options': '.*'},
    'properties': [{'id': 'noValue', 'value': '-'}],
}

# Auto-grid keys CUE cannot reject (its structs are open) that exist only on
# Grafana's main branch.
BANNED_AUTO_GRID_KEYS = (
    'fitContent',
    'matchRowHeights',
    'minHeight',
    'minHeightMode',
    'maxHeight',
    'maxHeightMode',
)

CHANGE_QUERY = re.compile(r'\b(i?delta|deriv)\(|-\s*first_over_time\(')

PROSE_CHARS = (
    ('\u2014', 'an em dash'),
    ('\u2013', 'an en dash'),
    (';', 'a semicolon'),
    ('\u201c', 'a curly quote'),
    ('\u201d', 'a curly quote'),
    ('\u2018', 'a curly quote'),
    ('\u2019', 'a curly quote'),
    ('\u2192', 'an arrow'),
)
ACRONYMS = {'GHCR', 'HTTP', 'HTTPS', 'HTML', 'JSON', 'YAML', 'TOML', 'UUID', 'SBOM', 'ASCII'}
ASIDES = {'(p50)', '(p90)', '(p95)', '(p99)'}
MAX_SENTENCE_WORDS = 35
MIN_DESCRIPTION_CHARS = 100


@dataclasses.dataclass(frozen=True)
class Finding:
    where: str
    rule: str
    message: str
    fix: str

    def __str__(self) -> str:
        return f'{self.where}: [{self.rule}] {self.message} Fix: {self.fix}'


class Checker:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def fail(self, where: str, rule: str, message: str, fix: str) -> None:
        self.findings.append(Finding(where, rule, message, fix))

    def envelope(self, doc: dict, base: tuple[str, str] | None) -> dict | None:
        where = 'envelope'
        if doc.get('apiVersion') != API_VERSION:
            self.fail(
                where,
                'envelope',
                f'apiVersion is {doc.get("apiVersion")!r}.',
                f'set "apiVersion": "{API_VERSION}".',
            )
        if doc.get('kind') != 'Dashboard':
            self.fail(
                where, 'envelope', f'kind is {doc.get("kind")!r}.', 'set "kind": "Dashboard".'
            )
        extra = sorted(set(doc) - set(ENVELOPE_KEYS))
        if extra:
            self.fail(
                where,
                'envelope',
                f'top-level keys {", ".join(extra)} are server output.',
                'keep only apiVersion, kind, metadata and spec.',
            )
        metadata = doc.get('metadata')
        name = metadata.get('name') if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not name:
            self.fail(
                where,
                'envelope',
                'metadata.name is missing or empty, so Grafana files the dashboard under a random UID.',
                'set metadata.name to the dashboard identity (a converted dashboard keeps its classic uid).',
            )
        if isinstance(metadata, dict) and set(metadata) - {'name'}:
            self.fail(
                where,
                'envelope',
                f'metadata holds {", ".join(sorted(set(metadata) - {"name"}))}.',
                'keep only metadata.name, because the provider owns the folder, namespace and versions.',
            )
        if base is not None and isinstance(name, str) and name and name != base[1]:
            field = 'metadata.name' if base[0] == 'v2' else 'uid'
            self.fail(
                where,
                'identity',
                f"metadata.name {name!r} differs from the base copy's {field} {base[1]!r}; "
                'a renamed dashboard provisions as a new one and leaves the old one behind.',
                f'set metadata.name back to {base[1]!r}.',
            )
        spec = doc.get('spec')
        if not isinstance(spec, dict):
            self.fail(
                where, 'envelope', 'spec is missing or not an object.', 'add the dashboard spec.'
            )
            return None
        return spec

    def walk(self, layout: object, where: str, ctx: Layout) -> None:
        if not isinstance(layout, dict):
            self.fail(where, 'references', 'layout is not an object.', 'use a RowsLayout.')
            return
        kind = layout.get('kind')
        spec = layout.get('spec') if isinstance(layout.get('spec'), dict) else {}
        if kind not in LAYOUT_KINDS:
            self.fail(
                where,
                'references',
                f'unknown layout kind {kind!r}; Grafana provisions it as an empty grid.',
                f'use one of {", ".join(sorted(LAYOUT_KINDS))}.',
            )
            return
        if kind == 'GridLayout':
            ctx.grids.append((where, [item for _, item in objs(spec.get('items'))]))
            for i, item in objs(spec.get('items')):
                item_spec = obj(item.get('spec'))
                at = f'{where}.items[{i}]'
                ctx.reference(item_spec.get('element'), at)
                ctx.conditional_judged.add(id(item_spec))
                if 'conditionalRendering' in item_spec:
                    self.fail(
                        at,
                        'layout',
                        'a GridLayoutItem carries no conditionalRendering; Grafana ignores it.',
                        'move the panel into an AutoGridLayout row, or put the rule on its row.',
                    )
        elif kind == 'AutoGridLayout':
            ctx.auto_grids.append((where, spec))
            for key in BANNED_AUTO_GRID_KEYS:
                if key in spec:
                    self.fail(
                        where,
                        'banned-field',
                        f'auto-grid {key} exists only on Grafana main, not in a release.',
                        f'remove {key}.',
                    )
            for i, item in objs(spec.get('items')):
                item_spec = obj(item.get('spec'))
                ctx.conditional_judged.add(id(item_spec))
                ctx.reference(item_spec.get('element'), f'{where}.items[{i}]')
        elif kind == 'RowsLayout':
            for i, row in objs(spec.get('rows')):
                row_spec = obj(row.get('spec'))
                ctx.conditional_judged.add(id(row_spec))
                at = f'{where}.rows[{i}]'
                title = row_spec.get('title')
                if not isinstance(title, str) or not title.strip():
                    self.fail(at, 'layout', 'the row has no title.', 'give every row a title.')
                else:
                    ctx.prose.append((f'{at} "{title}"', 'row title', title, False))
                ctx.variables.extend(
                    (f'{at}.variables', v) for _, v in objs(row_spec.get('variables'))
                )
                inner = obj(row_spec.get('layout')).get('kind')
                if inner in {'RowsLayout', 'TabsLayout'}:
                    self.fail(
                        at,
                        'layout',
                        f'the row holds a {inner}; a row holds panels, not rows or tabs.',
                        "make the row's layout a GridLayout or an AutoGridLayout.",
                    )
                self.walk(row_spec.get('layout'), f'{at}.layout', ctx)
        else:
            for i, tab in objs(spec.get('tabs')):
                tab_spec = obj(tab.get('spec'))
                ctx.conditional_judged.add(id(tab_spec))
                at = f'{where}.tabs[{i}]'
                if isinstance(tab_spec.get('title'), str):
                    ctx.prose.append((at, 'tab title', tab_spec['title'], False))
                ctx.variables.extend(
                    (f'{at}.variables', v) for _, v in objs(tab_spec.get('variables'))
                )
                self.walk(tab_spec.get('layout'), f'{at}.layout', ctx)

    def stray_conditionals(self, node: object, where: str, ctx: Layout) -> None:
        if isinstance(node, list):
            for i, member in enumerate(node):
                self.stray_conditionals(member, f'{where}[{i}]', ctx)
            return
        if not isinstance(node, dict):
            return
        if 'conditionalRendering' in node and id(node) not in ctx.conditional_judged:
            self.fail(
                where,
                'layout',
                'conditionalRendering sits outside an auto-grid item, a row or a tab, '
                'so Grafana ignores it.',
                "move the rule to the panel's AutoGridLayoutItem, its row or its tab.",
            )
        for key, value in node.items():
            self.stray_conditionals(value, f'{where}.{key}', ctx)

    def references(self, spec: dict, ctx: Layout) -> None:
        elements = spec.get('elements') if isinstance(spec.get('elements'), dict) else {}
        for name, at in ctx.refs:
            if name not in elements:
                self.fail(
                    at,
                    'references',
                    f'ElementReference {name!r} names no element; Grafana draws nothing there.',
                    'reference an existing spec.elements name.',
                )
        placed = {}
        for name, _ in ctx.refs:
            placed[name] = placed.get(name, 0) + 1
        ids: dict[object, str] = {}
        for name, element in elements.items():
            at = f'element {name}'
            count = placed.get(name, 0)
            if count == 0:
                self.fail(
                    at,
                    'references',
                    'the element is not placed in the layout.',
                    'place it once or delete it.',
                )
            elif count > 1:
                self.fail(
                    at,
                    'references',
                    f'the element is placed {count} times.',
                    'place it exactly once.',
                )
            pid = obj(element.get('spec')).get('id') if isinstance(element, dict) else None
            if name != f'panel-{pid}':
                self.fail(
                    at,
                    'references',
                    f'the element holds panel id {pid!r}, so its name should be panel-{pid}.',
                    'name every element panel-<spec.id>.',
                )
            if pid in ids:
                self.fail(
                    at,
                    'references',
                    f'panel id {pid} is also used by {ids[pid]}.',
                    'give every panel a unique, stable id so viewPanel links survive.',
                )
            else:
                ids[pid] = name

    def datasource(self, ref: object, where: str) -> None:
        fix = 'reference the datasource variable: {"name": "${datasource}"}.'
        if ref is None:
            self.fail(where, 'datasource', 'the query has no datasource.', fix)
        elif isinstance(ref, str):
            self.fail(
                where,
                'datasource',
                f'the datasource is a string {ref!r}, which Grafana mis-resolves (grafana/grafana#128167).',
                fix,
            )
        elif not isinstance(ref, dict) or not VARIABLE_REF.match(str(ref.get('name', ''))):
            name = ref.get('name') if isinstance(ref, dict) else ref
            self.fail(
                where,
                'datasource',
                f'the datasource {name!r} is not a variable reference, so it breaks on any other install.',
                fix,
            )
        if isinstance(ref, dict) and set(ref) - {'name'}:
            self.fail(
                where,
                'datasource',
                f'the datasource also holds {", ".join(sorted(set(ref) - {"name"}))}, '
                'which pins it to one install.',
                fix,
            )

    def variable(self, var: object, where: str, ctx: Layout) -> None:
        if not isinstance(var, dict):
            return
        kind = var.get('kind')
        spec = obj(var.get('spec'))
        name = spec.get('name', '?')
        at = f'{where} variable {name}'
        if kind in CLOSED_SET_VARIABLES and spec.get('allowCustomValue') is not False:
            self.fail(
                at,
                'standard',
                f'a {kind} accepts typed values unless allowCustomValue is false (the default is true).',
                'set "allowCustomValue": false.',
            )
        if (
            spec.get('hide', 'dontHide') not in UNLABELLED_HIDE
            and not str(spec.get('label') or '').strip()
        ):
            self.fail(
                at, 'standard', 'a visible variable has no label.', 'add a label in reader words.'
            )
        if kind == 'QueryVariable':
            self.datasource(obj(spec.get('query')).get('datasource'), at)
        elif kind in {'GroupByVariable', 'AdhocVariable'} and 'datasource' in var:
            self.datasource(var.get('datasource'), at)
        if isinstance(spec.get('label'), str):
            ctx.prose.append((at, 'variable label', spec['label'], False))
        if isinstance(spec.get('description'), str):
            ctx.prose.append((at, 'variable description', spec['description'], True))

    def panel(self, name: str, element: dict, ctx: Layout) -> None:
        if not isinstance(element, dict) or element.get('kind') != 'Panel':
            return
        spec = obj(element.get('spec'))
        title = spec.get('title') if isinstance(spec.get('title'), str) else ''
        at = f'element {name} "{title}"'
        viz = obj(spec.get('vizConfig'))
        ptype = viz.get('group')
        options = obj(obj(viz.get('spec')).get('options'))
        field_config = obj(obj(viz.get('spec')).get('fieldConfig'))
        dflt = obj(field_config.get('defaults'))
        overrides = [o for _, o in objs(field_config.get('overrides'))]
        data = obj(obj(spec.get('data')).get('spec'))
        queries = [obj(obj(q.get('spec')).get('query')) for _, q in objs(data.get('queries'))]

        if 'subtitle' in spec:
            self.fail(
                at,
                'banned-field',
                'Grafana 13.2 does not render a panel subtitle.',
                'fold it into the description.',
            )
        for i, query in enumerate(queries):
            self.datasource(query.get('datasource'), f'{at} query {i}')

        if not title.strip():
            self.fail(at, 'standard', 'the panel has no title.', 'give it a title in reader words.')
        else:
            ctx.prose.append((at, 'panel title', title, False))
        self.description(at, title, spec.get('description'))
        if isinstance(spec.get('description'), str):
            ctx.prose.append((at, 'panel description', spec['description'], True))

        if ptype in NO_VALUE_TYPES and not str(dflt.get('noValue') or '').strip():
            self.fail(
                at,
                'standard',
                f'a {ptype} panel has no noValue text.',
                'set fieldConfig.defaults.noValue to what an empty panel means, in reader words.',
            )
        if dflt.get('noValue') == '-':
            self.fail(
                at,
                'standard',
                'noValue is a bare "-", which tells the reader nothing.',
                'write what an empty panel means; a table puts "-" only in its cell override.',
            )
        if ptype == 'table' and TABLE_CELL_OVERRIDE not in overrides:
            self.fail(
                at,
                'standard',
                "a table's noValue also fills every empty cell.",
                'add the override {"matcher": {"id": "byRegexp", "options": ".*"}, '
                '"properties": [{"id": "noValue", "value": "-"}]}.',
            )
        self.thresholds(at, ptype, options, dflt, overrides, spec.get('description') or '')
        self.min_rule(at, ptype, options, dflt, queries)
        self.panel_prose(at, dflt, overrides, data, ctx)

    def description(self, at: str, title: str, desc: object) -> None:
        text = desc.strip() if isinstance(desc, str) else ''
        fix = 'write 2-4 sentences: what it shows, how to read it, what is abnormal, what no value means.'
        if len(text) < MIN_DESCRIPTION_CHARS:
            self.fail(
                at,
                'standard',
                f'the description has {len(text)} characters; it needs at least {MIN_DESCRIPTION_CHARS} characters.',
                fix,
            )
        count = len(sentences(text))
        if text and not 2 <= count <= 4:
            self.fail(
                at,
                'standard',
                f'the description has {count} sentence(s); it needs 2-4 sentences.',
                fix,
            )
        if text and text.lower() == title.strip().lower():
            self.fail(at, 'standard', 'the description restates the title.', fix)

    def thresholds(
        self, at: str, ptype: str, options: dict, dflt: dict, overrides: list, desc: str
    ) -> None:
        sets = []
        if isinstance(dflt.get('thresholds'), dict):
            sets.append(('defaults', dflt['thresholds']))
        for o in overrides:
            for _, prop in objs(o.get('properties')):
                if prop.get('id') == 'thresholds' and isinstance(prop.get('value'), dict):
                    sets.append(
                        (f'override {obj(o.get("matcher")).get("options")!r}', prop['value'])
                    )
        for where, config in sets:
            if config.get('mode') not in {'absolute', 'percentage'}:
                self.fail(
                    at,
                    'standard',
                    f'the {where} thresholds mode is {config.get("mode")!r}.',
                    'set "mode": "absolute" (or "percentage" when relative to min and max).',
                )
            steps = [step for _, step in objs(config.get('steps'))]
            if not steps or 'value' not in steps[0] or steps[0]['value'] not in (None, 0):
                self.fail(
                    at,
                    'standard',
                    f'the {where} thresholds base step has no explicit value of null or 0.',
                    'start the steps with {"value": null, "color": "<base colour>"}.',
                )

        named = []
        if thresholds_visible(ptype, options, dflt) and not dflt.get('mappings'):
            named.append(('defaults', [st for _, st in objs(dflt['thresholds'].get('steps'))]))
        if paints_thresholds(ptype, options, dflt):
            named.extend(
                (w, [st for _, st in objs(c.get('steps'))]) for w, c in sets if w != 'defaults'
            )
        lowered = desc.lower()
        for where, steps in named:
            for step in steps[1:]:
                value = step.get('value')
                if value is None:
                    continue
                colour = str(step.get('color', ''))
                if not any(s in desc for s in level_spellings(value, dflt.get('unit'))):
                    self.fail(
                        at,
                        'standard',
                        f'threshold level {value} ({colour}, {where}) is not named in the description.',
                        'name every visible threshold step by colour and level.',
                    )
                plain = re.sub(r'^(semi-dark|dark|light|super-light)-', '', colour)
                if plain.lower() not in lowered:
                    self.fail(
                        at,
                        'standard',
                        f'threshold colour {colour} at {value} ({where}) is not named in the description.',
                        'name every visible threshold step by colour and level.',
                    )

    def min_rule(self, at: str, ptype: str, options: dict, dflt: dict, queries: list) -> None:
        if ptype != 'timeseries' and not (ptype == 'stat' and options.get('graphMode') == 'area'):
            return
        exprs = [str(obj(q.get('spec')).get('expr') or '') for q in queries]
        if exprs and all(CHANGE_QUERY.search(e) for e in exprs):
            if 'min' in dflt:
                self.fail(
                    at,
                    'standard',
                    'every query is a change over time, which can go below zero, yet the panel sets min.',
                    'remove fieldConfig.defaults.min.',
                )
        elif not is_zero(dflt.get('min')):
            self.fail(
                at,
                'standard',
                'a series that cannot go negative needs min: 0, or its axis floats.',
                'set fieldConfig.defaults.min to 0.',
            )

    def panel_prose(self, at: str, dflt: dict, overrides: list, data: dict, ctx: Layout) -> None:
        texts = []
        if isinstance(dflt.get('noValue'), str):
            texts.append(('noValue', dflt['noValue']))
        mappings = [m for _, m in objs(dflt.get('mappings'))]
        for o in overrides:
            for _, prop in objs(o.get('properties')):
                if prop.get('id') == 'mappings':
                    mappings.extend(m for _, m in objs(prop.get('value')))
                elif prop.get('id') == 'displayName' and isinstance(prop.get('value'), str):
                    texts.append(('display name', prop['value']))
        for m in mappings:
            opts = obj(m.get('options'))
            results = opts.values() if m.get('type') == 'value' else [opts.get('result')]
            texts.extend(
                ('mapping text', r['text'])
                for r in results
                if isinstance(r, dict) and r.get('text')
            )
        for _, t in objs(data.get('transformations')):
            renames = obj(obj(obj(t.get('spec')).get('options')).get('renameByName'))
            texts.extend(('column header', v) for v in renames.values() if isinstance(v, str))
        ctx.prose.extend((at, kind, text, False) for kind, text in texts)

    def spec(self, spec: dict) -> None:
        ctx = Layout()
        layout = spec.get('layout')
        self.top_layout(layout)
        self.walk(layout, 'spec.layout', ctx)
        self.stray_conditionals(spec, 'spec', ctx)
        self.references(spec, ctx)

        elements = spec.get('elements') if isinstance(spec.get('elements'), dict) else {}
        for name, element in elements.items():
            self.panel(name, element, ctx)
        types = {
            name: obj(obj(e.get('spec')).get('vizConfig')).get('group')
            for name, e in elements.items()
            if isinstance(e, dict)
        }
        self.layout_order(ctx)
        self.cursor_sync(spec, ctx, types)

        for i, annotation in objs(spec.get('annotations')):
            a_spec = obj(annotation.get('spec'))
            if not a_spec.get('builtIn'):
                self.datasource(
                    obj(a_spec.get('query')).get('datasource'), f'spec.annotations[{i}]'
                )
        ctx.variables[:0] = (('spec.variables', v) for _, v in objs(spec.get('variables')))
        for where, var in ctx.variables:
            self.variable(var, where, ctx)
        self.dashboard(spec, ctx)
        for where, kind, text, is_desc in ctx.prose:
            self.prose(where, kind, text, is_desc)

    def top_layout(self, layout: object) -> None:
        kind = layout.get('kind') if isinstance(layout, dict) else layout
        if kind == 'RowsLayout':
            return
        if kind != 'TabsLayout':
            self.fail(
                'spec.layout',
                'layout',
                f'spec.layout is {kind!r}; the standard puts every panel in a titled row.',
                'make spec.layout a RowsLayout, or a TabsLayout whose tabs each hold one.',
            )
            return
        for i, tab in objs(obj(layout.get('spec')).get('tabs')):
            tab_spec = obj(tab.get('spec'))
            at = f'spec.layout.tabs[{i}]'
            title = tab_spec.get('title')
            if not isinstance(title, str) or not title.strip():
                self.fail(at, 'layout', 'the tab has no title.', 'give every tab a title.')
            inner = obj(tab_spec.get('layout')).get('kind')
            if inner != 'RowsLayout':
                self.fail(
                    at,
                    'layout',
                    f'tabs[{i}] holds a {inner}; the standard puts every panel in a titled row.',
                    "make the tab's layout a RowsLayout.",
                )

    def layout_order(self, ctx: Layout) -> None:
        for where, items in ctx.grids:
            keys = [(obj(i.get('spec')).get('y', 0), obj(i.get('spec')).get('x', 0)) for i in items]
            if keys != sorted(keys):
                self.fail(
                    where,
                    'layout',
                    'grid items are not listed in y-then-x order, so the file order differs from the reading order.',
                    'sort the items by y, then x.',
                )

    def cursor_sync(self, spec: dict, ctx: Layout, types: dict) -> None:
        if spec.get('cursorSync') == 'Crosshair':
            return
        for where, items in ctx.grids:
            boxes = []
            for item in items:
                s = obj(item.get('spec'))
                if types.get(obj(s.get('element')).get('name')) == 'timeseries':
                    boxes.append(
                        (s.get('x', 0), s.get('y', 0), s.get('width', 0), s.get('height', 0))
                    )
            if any(side_by_side(a, b) for i, a in enumerate(boxes) for b in boxes[i + 1 :]):
                self.need_crosshair(where)
                return
        for where, grid in ctx.auto_grids:
            names = [
                obj(obj(i.get('spec')).get('element')).get('name')
                for _, i in objs(grid.get('items'))
            ]
            series = sum(1 for n in names if types.get(n) == 'timeseries')
            if series >= 2 and grid.get('maxColumnCount', 3) > 1:
                self.need_crosshair(where)
                return

    def need_crosshair(self, where: str) -> None:
        self.fail(
            where,
            'layout',
            'time series sit side by side without a shared crosshair.',
            'set spec.cursorSync to "Crosshair".',
        )

    def dashboard(self, spec: dict, ctx: Layout) -> None:
        for key in ('title', 'description'):
            value = spec.get(key)
            if not isinstance(value, str) or not value.strip():
                self.fail(
                    'spec',
                    'standard',
                    f'the dashboard {key} is missing.',
                    f'set spec.{key}; the description shows in the dashboard list.',
                )
            else:
                ctx.prose.append((f'spec.{key}', f'dashboard {key}', value, key == 'description'))
        tags = spec.get('tags')
        if not isinstance(tags, list) or not tags:
            self.fail(
                'spec.tags',
                'standard',
                'the dashboard has no tags.',
                'set spec.tags to lowercase domain words, for example ["media", "plex"].',
            )
            tags = []
        for tag in tags:
            if not isinstance(tag, str) or tag != tag.lower() or tag in DATASOURCE_TAGS:
                self.fail(
                    'spec.tags',
                    'standard',
                    f'tag {tag!r} is not a lowercase domain word, or names a datasource type.',
                    'use lowercase domain words; never loki, prometheus or mimir.',
                )
        timezone = obj(spec.get('timeSettings')).get('timezone')
        if timezone != 'browser':
            self.fail(
                'spec.timeSettings',
                'standard',
                f'timeSettings.timezone is {timezone!r}.',
                'set it to "browser" so times show in the reader\'s zone.',
            )

    def prose(self, where: str, kind: str, text: str, is_desc: bool) -> None:
        plain = strip_code(text)
        fix = 'rewrite it in plain words (writing.md "Public docs").'
        for char, name in PROSE_CHARS:
            if char in plain:
                self.fail(where, 'prose', f'the {kind} has {name}: {text!r}.', fix)
        if re.search(r"n't\b", plain):
            self.fail(where, 'prose', f'the {kind} has a negative contraction: {text!r}.', fix)
        for word in re.findall(r'\b[A-Z]{4,}\b', plain):
            if word not in ACRONYMS:
                self.fail(
                    where, 'prose', f'the {kind} has the ALL-CAPS word {word}: {text!r}.', fix
                )
        if not is_desc:
            return
        for aside in re.findall(r'\([^)]*\)', plain):
            if aside not in ASIDES:
                self.fail(where, 'prose', f'the {kind} has the bracketed aside {aside}.', fix)
        for sentence in sentences(plain):
            words = len(sentence.split())
            if words > MAX_SENTENCE_WORDS:
                self.fail(
                    where,
                    'prose',
                    f'a {kind} sentence is over {MAX_SENTENCE_WORDS} words ({words}): {sentence!r}.',
                    fix,
                )


@dataclasses.dataclass
class Layout:
    refs: list = dataclasses.field(default_factory=list)
    grids: list = dataclasses.field(default_factory=list)
    auto_grids: list = dataclasses.field(default_factory=list)
    variables: list = dataclasses.field(default_factory=list)
    prose: list = dataclasses.field(default_factory=list)
    conditional_judged: set = dataclasses.field(default_factory=set)

    def reference(self, ref: object, where: str) -> None:
        name = ref.get('name') if isinstance(ref, dict) else None
        self.refs.append((name, where))


def thresholds_visible(ptype: str, options: dict, dflt: dict) -> bool:
    steps = objs(obj(dflt.get('thresholds')).get('steps'))
    return len(steps) >= 2 and paints_thresholds(ptype, options, dflt)


def paints_thresholds(ptype: str, options: dict, dflt: dict) -> bool:
    if ptype == 'timeseries':
        return obj(obj(dflt.get('custom')).get('thresholdsStyle')).get('mode', 'off') != 'off'
    if ptype in {'stat', 'gauge', 'bargauge'}:
        if options.get('colorMode') == 'none':
            return False
        return obj(dflt.get('color')).get('mode', 'thresholds') == 'thresholds'
    return False


def level_spellings(value: float, unit: object) -> set[str]:
    out = {str(value), f'{value:,}' if isinstance(value, int) else str(value)}
    if isinstance(value, float) and value.is_integer():
        out |= {str(int(value)), f'{int(value):,}'}
    if unit == 'percentunit':
        out.add(f'{round(value * 100):g}%')
    if unit == 'percent':
        out.add(f'{value:g}%')
    if unit == 's' and value % 60 == 0:
        minutes = int(value // 60)
        out.add('1 minute' if minutes == 1 else f'{minutes} minutes')
    return out


def side_by_side(a: tuple, b: tuple) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ay < by + bh and by < ay + ah and (ax + aw <= bx or bx + bw <= ax)


def obj(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def objs(value: object) -> list[tuple[int, dict]]:
    """Non-object members are skipped: the CUE vet reports them."""
    return (
        [(i, m) for i, m in enumerate(value) if isinstance(m, dict)]
        if isinstance(value, list)
        else []
    )


def is_zero(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def strip_code(text: str) -> str:
    text = re.sub(r'`[^`]*`', '', text)
    return re.sub(r'\]\([^)]*\)', ']', text)


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s]


def identity(doc: object) -> tuple[str, str] | None:
    if not isinstance(doc, dict):
        return None
    if 'apiVersion' in doc:
        probe = Checker()
        if probe.envelope(doc, None) is None or probe.findings:
            return None
        return ('v2', doc['metadata']['name'])
    if is_classic(doc):
        return ('classic', doc['uid'])
    return None


def is_classic(doc: dict) -> bool:
    return 'apiVersion' not in doc and isinstance(doc.get('uid'), str) and bool(doc['uid'])


def check(
    doc: object, base: object = None, vet_spec: Callable[[], list[Finding]] | None = None
) -> tuple[list[Finding], list[str]]:
    """The spec rules run only on a spec vet_spec accepts: they read schema-typed fields unguarded."""
    checker = Checker()
    base_id = identity(base)
    if not isinstance(doc, dict):
        checker.fail(
            'envelope',
            'envelope',
            'the file is not a JSON object.',
            'export the V2 Resource model.',
        )
        return checker.findings, []
    if 'error' in doc and 'spec' not in doc:
        checker.fail(
            'envelope',
            'envelope',
            'the file is a Grafana export error object, not a dashboard (grafana/grafana#129202).',
            'export the dashboard again and check the file holds a spec.',
        )
        return checker.findings, []
    if is_classic(doc):
        if base_id is not None and base_id[0] == 'v2':
            checker.fail(
                'envelope',
                'classic',
                'the file regresses to the classic schema; the base copy is schema v2.',
                'keep the dashboard a dashboard.grafana.app/v2 resource.',
            )
            return checker.findings, []
        return [], ['classic dashboard schema: not checked; convert it to schema v2']
    if 'apiVersion' not in doc:
        checker.fail(
            'envelope',
            'envelope',
            'the file is neither a classic dashboard (a string uid) nor a schema v2 resource.',
            f'wrap the spec as {{"apiVersion": "{API_VERSION}", "kind": "Dashboard", "metadata": {{"name": ...}}, "spec": ...}}.',
        )
        return checker.findings, []
    spec = checker.envelope(doc, base_id)
    if spec is None:
        return checker.findings, []
    if vet_spec is not None:
        vetted = vet_spec()
        if vetted:
            return checker.findings + vetted, []
    checker.spec(spec)
    return checker.findings, []


STRICT_CUE = """package {package}

// The stock elements default `Element | *{{}}` lets a garbage element pass.
StrictDashboardSpec: DashboardSpec & {{elements: [string]: Element}}
StrictDashboardResource: {{spec: StrictDashboardSpec}}
"""


def vet(path: pathlib.Path, cue: str, schemas: list[tuple[str, pathlib.Path]]) -> list[Finding]:
    """Finding locations point into the checked file, not the strict wrapper."""
    findings = []
    if '/' in cue:
        cue = str(pathlib.Path(cue).resolve())
    with tempfile.TemporaryDirectory() as tmp:
        for tag, schema in schemas:
            schema = schema.resolve()
            try:
                text = schema.read_text(encoding='utf-8')
                match = re.search(r'^package\s+(\w+)', text, re.MULTILINE)
                strict = pathlib.Path(tmp) / f'strict-{tag}.cue'
                strict.write_text(STRICT_CUE.format(package=match.group(1) if match else 'v2'))
                result = subprocess.run(
                    [
                        cue,
                        'vet',
                        '-c',
                        '-d',
                        'StrictDashboardResource',
                        str(schema),
                        str(strict),
                        path.name,
                    ],
                    cwd=path.parent,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as err:
                findings.append(
                    Finding(
                        'spec',
                        'cue',
                        f'cue vet against {tag} did not run: {err}.',
                        'check --cue and --schema.',
                    )
                )
                continue
            if result.returncode != 0:
                findings.append(
                    Finding(
                        'spec',
                        'cue',
                        f'the spec does not vet against Grafana {tag} dashboard_spec.cue: '
                        + cue_summary(result.stdout + result.stderr, path.name),
                        f'use only fields and values the {tag} schema defines.',
                    )
                )
    return findings


def cue_summary(output: str, name: str, limit: int = 12) -> str:
    """Each error headline with its first location inside the checked file."""
    lines, out = output.splitlines(), []
    for i, line in enumerate(lines):
        if line.startswith(' ') or not line.strip():
            continue
        location = next(
            (s.strip() for s in lines[i + 1 :] if s.startswith(' ') and name in s),
            '',
        )
        out.append(f'{line.strip()} ({location})' if location else line.strip())
        if len(out) == limit:
            out.append('...')
            break
    return ' | '.join(out) or output.strip()


def escape_data(text: str) -> str:
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def escape_property(text: str) -> str:
    return escape_data(text).replace(':', '%3A').replace(',', '%2C')


def load(path: pathlib.Path) -> tuple[object, str | None]:
    try:
        return json.loads(path.read_text(encoding='utf-8')), None
    except FileNotFoundError:
        return None, 'the file does not exist.'
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        return None, f'the file is not valid JSON: {err}.'


def parse_schema(value: str) -> tuple[str, pathlib.Path]:
    tag, sep, file = value.partition('=')
    if not sep or not tag or not file:
        raise argparse.ArgumentTypeError(f'expected TAG=FILE, got {value!r}')
    return tag, pathlib.Path(file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--github', action='store_true', help='print GitHub workflow annotations')
    parser.add_argument('--cue', required=True, help='path to the cue binary')
    parser.add_argument(
        '--schema', action='append', required=True, type=parse_schema, metavar='TAG=FILE'
    )
    parser.add_argument(
        '--base-file', type=pathlib.Path, help="the base branch's copy, if it has one"
    )
    parser.add_argument('files', nargs='+', type=pathlib.Path, metavar='FILE')
    args = parser.parse_args(argv)
    if args.base_file is not None and len(args.files) > 1:
        parser.error('--base-file applies to exactly one FILE')

    base, base_error = None, None
    if args.base_file is not None:
        base, base_error = load(args.base_file)
        if base_error is None and identity(base) is None:
            base_error = (
                'the file is neither a classic dashboard nor a complete schema v2 resource.'
            )
    failed = False
    for path in args.files:
        doc, error = load(path)
        if base_error:
            found, notices = (
                [
                    Finding(
                        'base copy',
                        'identity',
                        f'the base copy cannot be used: {base_error} '
                        'Without it the identity and regression checks cannot run.',
                        'restore a valid dashboard file on the base branch.',
                    )
                ],
                [],
            )
        elif error:
            found, notices = (
                [Finding('file', 'envelope', error, 'commit a valid dashboard JSON file.')],
                [],
            )
        else:
            found, notices = check(doc, base, lambda p=path: vet(p, args.cue, args.schema))
        for note in notices:
            if args.github:
                print(f'::notice file={escape_property(str(path))}::{escape_data(note)}')
            else:
                print(f'{path}: notice: {note}')
        for finding in found:
            if args.github:
                title = escape_property(f'dashboard-check {finding.rule}')
                print(
                    f'::error file={escape_property(str(path))},title={title}::{escape_data(str(finding))}'
                )
            else:
                print(f'{path}: {finding}')
        if found:
            failed = True
            print(f'{path}: {len(found)} finding(s)', file=sys.stderr)
        elif not notices:
            tags = ', '.join(tag for tag, _ in args.schema)
            print(f'{path}: ok (schema v2, vetted against {tags})')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
