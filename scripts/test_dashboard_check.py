"""testdata/dashboard-check/valid.json must pass every check: each rule test mutates a copy.

The CUE tests run only when DASHBOARD_CHECK_CUE (the cue binary) and
DASHBOARD_CHECK_SCHEMAS (the directory install.sh filled) are set.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from dashboard_check_fixtures import (
    CLASSIC,
    STRAY_CONDITIONAL_PLACES,
    conditional_group,
    dc,
    defaults,
    element,
    grid_items,
    rows,
    run_cli,
    tabs_of,
    valid,
    variable,
)

CUE = os.environ.get('DASHBOARD_CHECK_CUE', '')
SCHEMAS = os.environ.get('DASHBOARD_CHECK_SCHEMAS', '')


class RuleCase(unittest.TestCase):
    def findings(self, doc: dict, base: dict | None = None) -> list:
        found, _ = dc.check(doc, base)
        return found

    def assert_finding(self, doc: dict, rule: str, text: str, base: dict | None = None) -> None:
        found = self.findings(doc, base)
        hits = [f for f in found if f.rule == rule and text in str(f)]
        self.assertTrue(
            hits, f'no {rule} finding containing {text!r}; got {[str(f) for f in found]}'
        )

    def assert_clean(self, doc: dict, base: dict | None = None) -> None:
        self.assertEqual([str(f) for f in self.findings(doc, base)], [])


class Envelope(RuleCase):
    def test_valid_fixture_has_no_findings(self):
        self.assert_clean(valid())

    def test_rejects_grafana_export_error_object(self):
        self.assert_finding({'error': 'Dashboard not found'}, 'envelope', 'export error object')

    def test_rejects_wrong_api_version(self):
        doc = valid()
        doc['apiVersion'] = 'dashboard.grafana.app/v2beta1'
        self.assert_finding(doc, 'envelope', 'apiVersion')

    def test_rejects_wrong_kind(self):
        doc = valid()
        doc['kind'] = 'DashboardWithAccessInfo'
        self.assert_finding(doc, 'envelope', 'kind')

    def test_rejects_metadata_beyond_name(self):
        doc = valid()
        doc['metadata']['namespace'] = 'default'
        self.assert_finding(doc, 'envelope', 'namespace')

    def test_rejects_empty_metadata_name(self):
        doc = valid()
        doc['metadata']['name'] = ''
        self.assert_finding(doc, 'envelope', 'metadata.name')

    def test_rejects_server_status_block(self):
        doc = valid()
        doc['status'] = {'conversion': {'failed': False}}
        self.assert_finding(doc, 'envelope', 'status')

    def test_rejects_a_file_of_neither_shape(self):
        self.assert_finding({'title': 'Job queue'}, 'envelope', 'neither')

    def test_rejects_a_root_that_is_not_an_object(self):
        self.assert_finding([valid()], 'envelope', 'not a JSON object')

    def test_rejects_a_spec_that_is_not_an_object(self):
        doc = valid()
        doc['spec'] = [doc['spec']]
        self.assert_finding(doc, 'envelope', 'spec is missing or not an object')

    def test_malformed_members_raise_no_error(self):
        for path in ('row', 'query', 'step', 'tags'):
            with self.subTest(member=path):
                doc = valid()
                if path == 'row':
                    rows(doc).append('row')
                elif path == 'query':
                    element(doc, 'panel-2')['data']['spec']['queries'].append(7)
                elif path == 'step':
                    defaults(doc, 'panel-1')['thresholds']['steps'].append('red')
                else:
                    doc['spec']['tags'] = 7
                self.findings(doc)


class Identity(RuleCase):
    def test_rejects_name_changed_from_v2_base(self):
        base = valid()
        doc = valid()
        doc['metadata']['name'] = 'job-queue-2'
        self.assert_finding(doc, 'identity', 'job-queue', base)

    def test_rejects_name_differing_from_classic_base_uid(self):
        doc = valid()
        self.assert_finding(doc, 'identity', 'other-uid', dict(CLASSIC, uid='other-uid'))

    def test_accepts_name_equal_to_classic_base_uid(self):
        self.assert_clean(valid(), CLASSIC)


class ClassicSchema(RuleCase):
    def test_classic_file_without_base_passes_with_notice(self):
        found, notices = dc.check(copy.deepcopy(CLASSIC), None)
        self.assertEqual(found, [])
        self.assertTrue(any('convert it to schema v2' in n for n in notices), notices)

    def test_classic_file_after_classic_base_passes_with_notice(self):
        found, notices = dc.check(copy.deepcopy(CLASSIC), copy.deepcopy(CLASSIC))
        self.assertEqual(found, [])
        self.assertTrue(notices)

    def test_classic_file_after_v2_base_regresses(self):
        self.assert_finding(copy.deepcopy(CLASSIC), 'classic', 'regresses', valid())


class References(RuleCase):
    def test_rejects_dangling_element_reference(self):
        doc = valid()
        grid_items(doc)[0]['spec']['element']['name'] = 'panel-99'
        self.assert_finding(doc, 'references', 'panel-99')

    def test_rejects_unreferenced_element(self):
        doc = valid()
        del grid_items(doc)[2]
        self.assert_finding(doc, 'references', 'not placed')

    def test_rejects_element_referenced_twice(self):
        doc = valid()
        grid_items(doc)[2]['spec']['element']['name'] = 'panel-2'
        self.assert_finding(doc, 'references', 'placed 2 times')

    def test_rejects_element_name_not_matching_panel_id(self):
        doc = valid()
        element(doc, 'panel-3')['id'] = 30
        self.assert_finding(doc, 'references', 'panel-30')

    def test_rejects_duplicate_panel_id(self):
        doc = valid()
        element(doc, 'panel-3')['id'] = 2
        self.assert_finding(doc, 'references', 'also used by panel-2')

    def test_rejects_unknown_layout_kind(self):
        doc = valid()
        rows(doc)[1]['spec']['layout']['kind'] = 'FlexLayout'
        self.assert_finding(doc, 'references', 'FlexLayout')


class BannedFields(RuleCase):
    def test_rejects_panel_subtitle(self):
        doc = valid()
        element(doc, 'panel-1')['subtitle'] = 'Right now'
        self.assert_finding(doc, 'banned-field', 'subtitle')

    def test_rejects_main_branch_auto_grid_fields(self):
        for field in (
            'fitContent',
            'matchRowHeights',
            'minHeight',
            'minHeightMode',
            'maxHeight',
            'maxHeightMode',
        ):
            with self.subTest(field=field):
                doc = valid()
                rows(doc)[1]['spec']['layout']['spec'][field] = 1
                self.assert_finding(doc, 'banned-field', field)

    def test_accepts_fields_added_in_grafana_13_2(self):
        doc = valid()
        defaults(doc, 'panel-2')['color'] = {
            'mode': 'gradient',
            'fixedColor': 'blue',
            'gradientColorTo': 'red',
        }
        defaults(doc, 'panel-1')['thresholds']['steps'][1]['valueExpr'] = '$limit'
        element(doc, 'panel-3')['data']['spec']['queryOptions']['timeTo'] = 'now-1h'
        self.assert_clean(doc)


class Datasources(RuleCase):
    def test_rejects_string_datasource_reference(self):
        doc = valid()
        element(doc, 'panel-2')['data']['spec']['queries'][0]['spec']['query']['datasource'] = (
            '${datasource}'
        )
        self.assert_finding(doc, 'datasource', 'a string')

    def test_rejects_literal_datasource_uid(self):
        doc = valid()
        element(doc, 'panel-2')['data']['spec']['queries'][0]['spec']['query']['datasource'] = {
            'name': 'mimir'
        }
        self.assert_finding(doc, 'datasource', 'mimir')

    def test_rejects_literal_uid_beside_the_variable_name(self):
        doc = valid()
        element(doc, 'panel-2')['data']['spec']['queries'][0]['spec']['query']['datasource'] = {
            'name': '${datasource}',
            'uid': 'hard-coded-uid',
        }
        self.assert_finding(doc, 'datasource', 'also holds uid')

    def test_rejects_query_without_datasource(self):
        doc = valid()
        del element(doc, 'panel-2')['data']['spec']['queries'][0]['spec']['query']['datasource']
        self.assert_finding(doc, 'datasource', 'no datasource')

    def test_rejects_variable_with_literal_datasource(self):
        doc = valid()
        variable(doc, 'worker')['spec']['query']['datasource'] = {'name': 'P1809F7CD0C75ACF3'}
        self.assert_finding(doc, 'datasource', 'P1809F7CD0C75ACF3')

    def test_accepts_the_built_in_annotation(self):
        doc = valid()
        self.assertEqual(
            doc['spec']['annotations'][0]['spec']['query']['datasource']['name'], '-- Grafana --'
        )
        self.assert_clean(doc)


class Layout(RuleCase):
    def test_rejects_top_level_layout_other_than_rows(self):
        doc = valid()
        doc['spec']['layout'] = rows(doc)[0]['spec']['layout']
        self.assert_finding(doc, 'layout', 'RowsLayout')

    def test_rejects_untitled_row(self):
        doc = valid()
        rows(doc)[1]['spec']['title'] = ' '
        self.assert_finding(doc, 'layout', 'no title')

    def test_accepts_top_level_tabs_of_titled_rows(self):
        doc = valid()
        doc['spec']['layout'] = tabs_of(('Overview', doc['spec']['layout']))
        self.assert_clean(doc)

    def test_rejects_untitled_tab(self):
        doc = valid()
        doc['spec']['layout'] = tabs_of((' ', doc['spec']['layout']))
        self.assert_finding(doc, 'layout', 'the tab has no title')

    def test_rejects_tab_whose_layout_is_not_rows(self):
        doc = valid()
        rows_layout = doc['spec']['layout']
        doc['spec']['layout'] = tabs_of(
            ('Overview', rows_layout['spec']['rows'][0]['spec']['layout'])
        )
        rows_layout['spec']['rows'] = rows_layout['spec']['rows'][1:]
        doc['spec']['layout']['spec']['tabs'].append(
            {'kind': 'TabsLayoutTab', 'spec': {'title': 'More', 'layout': rows_layout}}
        )
        self.assert_finding(doc, 'layout', 'tabs[0] holds a GridLayout')

    def test_rejects_rows_or_tabs_inside_a_row(self):
        for inner in ('RowsLayout', 'TabsLayout'):
            with self.subTest(inner=inner):
                doc = valid()
                row = rows(doc)[0]['spec']
                grid = row['layout']
                if inner == 'RowsLayout':
                    row['layout'] = {
                        'kind': 'RowsLayout',
                        'spec': {
                            'rows': [
                                {
                                    'kind': 'RowsLayoutRow',
                                    'spec': {'title': 'Inner', 'layout': grid},
                                }
                            ]
                        },
                    }
                else:
                    row['layout'] = tabs_of(('Inner', grid))
                found = self.findings(doc)
                self.assertTrue(
                    any(f.rule == 'layout' and f'holds a {inner}' in str(f) for f in found),
                    [str(f) for f in found],
                )
                self.assertFalse(
                    [f for f in found if f.rule == 'references'], [str(f) for f in found]
                )

    def test_rejects_grid_items_out_of_order(self):
        doc = valid()
        items = grid_items(doc)
        items[1], items[2] = items[2], items[1]
        self.assert_finding(doc, 'layout', 'y-then-x')

    def test_rejects_conditional_rendering_on_grid_item(self):
        doc = valid()
        grid_items(doc)[0]['spec']['conditionalRendering'] = {'kind': 'ConditionalRenderingGroup'}
        self.assert_finding(doc, 'layout', 'conditionalRendering')

    def test_rejects_conditional_rendering_outside_an_item_row_or_tab(self):
        for place, holder in STRAY_CONDITIONAL_PLACES.items():
            with self.subTest(place=place):
                doc = valid()
                holder(doc)['conditionalRendering'] = conditional_group()
                self.assert_finding(doc, 'layout', 'outside an auto-grid item')

    def test_accepts_conditional_rendering_on_a_row(self):
        doc = valid()
        rows(doc)[0]['spec']['conditionalRendering'] = conditional_group()
        self.assert_clean(doc)

    def test_accepts_conditional_rendering_on_a_tab(self):
        doc = valid()
        doc['spec']['layout'] = tabs_of(('Overview', doc['spec']['layout']))
        doc['spec']['layout']['spec']['tabs'][0]['spec']['conditionalRendering'] = (
            conditional_group()
        )
        self.assert_clean(doc)

    def test_accepts_conditional_rendering_on_auto_grid_item(self):
        doc = valid()
        rows(doc)[1]['spec']['layout']['spec']['items'][0]['spec']['conditionalRendering'] = {
            'kind': 'ConditionalRenderingGroup',
            'spec': {'visibility': 'show', 'condition': 'and', 'items': []},
        }
        self.assert_clean(doc)

    def test_requires_crosshair_for_side_by_side_time_series(self):
        doc = valid()
        doc['spec']['cursorSync'] = 'Off'
        self.assert_finding(doc, 'layout', 'Crosshair')

    def test_stacked_time_series_need_no_crosshair(self):
        doc = valid()
        doc['spec']['cursorSync'] = 'Off'
        items = grid_items(doc)
        items[2]['spec'].update(x=0, y=12, width=24)
        items[1]['spec']['width'] = 24
        self.assert_clean(doc)

    def test_requires_crosshair_for_time_series_sharing_an_auto_grid(self):
        doc = valid()
        doc['spec']['cursorSync'] = 'Off'
        items = grid_items(doc)
        items[2]['spec'].update(x=0, y=12, width=24)
        items[1]['spec']['width'] = 24
        element(doc, 'panel-4')['vizConfig']['group'] = 'timeseries'
        defaults(doc, 'panel-4')['min'] = 0
        self.assert_finding(doc, 'layout', 'Crosshair')


class HouseStandard(RuleCase):
    def test_rejects_missing_dashboard_title(self):
        doc = valid()
        doc['spec']['title'] = ''
        self.assert_finding(doc, 'standard', 'dashboard title')

    def test_rejects_missing_dashboard_description(self):
        doc = valid()
        del doc['spec']['description']
        self.assert_finding(doc, 'standard', 'dashboard description')

    def test_rejects_missing_panel_title(self):
        doc = valid()
        element(doc, 'panel-2')['title'] = ''
        self.assert_finding(doc, 'standard', 'no title')

    def test_rejects_short_panel_description(self):
        doc = valid()
        element(doc, 'panel-2')['description'] = 'Jobs per second. A gap means no data.'
        self.assert_finding(doc, 'standard', 'at least 100 characters')

    def test_rejects_single_sentence_description(self):
        doc = valid()
        element(doc, 'panel-2')['description'] = (
            'Jobs each worker finished per second averaged over five minutes, where a line '
            'that drops to zero means that worker has stopped taking work from the queue.'
        )
        self.assert_finding(doc, 'standard', '2-4 sentences')

    def test_rejects_description_over_four_sentences(self):
        doc = valid()
        element(doc, 'panel-2')['description'] += ' One more. And another.'
        self.assert_finding(doc, 'standard', '2-4 sentences')

    def test_rejects_description_restating_title(self):
        doc = valid()
        element(doc, 'panel-2')['title'] = element(doc, 'panel-2')['description']
        self.assert_finding(doc, 'standard', 'restates the title')

    def test_rejects_stat_without_no_value(self):
        doc = valid()
        del defaults(doc, 'panel-1')['noValue']
        self.assert_finding(doc, 'standard', 'noValue')

    def test_rejects_bare_dash_no_value(self):
        doc = valid()
        defaults(doc, 'panel-4')['noValue'] = '-'
        self.assert_finding(doc, 'standard', 'bare "-"')

    def test_rejects_table_without_cell_dash_override(self):
        doc = valid()
        element(doc, 'panel-4')['vizConfig']['spec']['fieldConfig']['overrides'] = []
        self.assert_finding(doc, 'standard', 'byRegexp')

    def test_rejects_query_variable_allowing_custom_values(self):
        doc = valid()
        variable(doc, 'worker')['spec']['allowCustomValue'] = True
        self.assert_finding(doc, 'standard', 'allowCustomValue')

    def test_rejects_custom_variable_left_at_the_default(self):
        doc = valid()
        doc['spec']['variables'].append(
            {
                'kind': 'CustomVariable',
                'spec': {
                    'name': 'mode',
                    'query': 'a,b',
                    'current': {'text': 'a', 'value': 'a'},
                    'label': 'Mode',
                    'hide': 'dontHide',
                },
            }
        )
        self.assert_finding(doc, 'standard', 'allowCustomValue')

    def test_text_variable_is_exempt_from_the_closed_set_rule(self):
        doc = valid()
        self.assertNotIn('allowCustomValue', variable(doc, 'window')['spec'])
        self.assert_clean(doc)

    def test_rejects_visible_variable_without_label(self):
        doc = valid()
        del variable(doc, 'window')['spec']['label']
        self.assert_finding(doc, 'standard', 'label')

    def test_hidden_variable_needs_no_label(self):
        doc = valid()
        del variable(doc, 'window')['spec']['label']
        variable(doc, 'window')['spec']['hide'] = 'hideVariable'
        self.assert_clean(doc)

    def test_rejects_row_variable_without_label(self):
        doc = valid()
        rows(doc)[1]['spec']['variables'] = [copy.deepcopy(variable(doc, 'window'))]
        del rows(doc)[1]['spec']['variables'][0]['spec']['label']
        self.assert_finding(doc, 'standard', 'label')

    def test_rejects_threshold_level_not_named(self):
        doc = valid()
        defaults(doc, 'panel-1')['thresholds']['steps'][1]['value'] = 90
        self.assert_finding(doc, 'standard', 'level 90')

    def test_rejects_threshold_colour_not_named(self):
        doc = valid()
        defaults(doc, 'panel-1')['thresholds']['steps'][1]['color'] = 'dark-orange'
        self.assert_finding(doc, 'standard', 'colour dark-orange')

    def test_rejects_override_threshold_level_not_named(self):
        doc = valid()
        element(doc, 'panel-1')['vizConfig']['spec']['fieldConfig']['overrides'] = [
            {
                'matcher': {'id': 'byName', 'options': 'a'},
                'properties': [
                    {
                        'id': 'thresholds',
                        'value': {
                            'mode': 'absolute',
                            'steps': [
                                {'value': None, 'color': 'green'},
                                {'value': 55, 'color': 'red'},
                            ],
                        },
                    }
                ],
            }
        ]
        self.assert_finding(doc, 'standard', 'level 55')

    def test_mappings_hide_the_default_thresholds(self):
        doc = valid()
        defaults(doc, 'panel-1')['thresholds']['steps'][1]['value'] = 90
        defaults(doc, 'panel-1')['mappings'] = [
            {'type': 'value', 'options': {'0': {'text': 'Empty'}}}
        ]
        self.assert_clean(doc)

    def test_rejects_empty_thresholds_mode(self):
        doc = valid()
        defaults(doc, 'panel-1')['thresholds']['mode'] = ''
        self.assert_finding(doc, 'standard', 'mode')

    def test_rejects_override_base_step_without_value(self):
        doc = valid()
        element(doc, 'panel-2')['vizConfig']['spec']['fieldConfig']['overrides'] = [
            {
                'matcher': {'id': 'byName', 'options': 'a'},
                'properties': [
                    {
                        'id': 'thresholds',
                        'value': {'mode': 'absolute', 'steps': [{'color': 'green'}]},
                    }
                ],
            }
        ]
        self.assert_finding(doc, 'standard', 'base step')

    def test_rejects_change_panel_with_min(self):
        doc = valid()
        defaults(doc, 'panel-5')['min'] = 0
        self.assert_finding(doc, 'standard', 'change over time')

    def test_first_over_time_subtraction_is_a_change(self):
        doc = valid()
        query = element(doc, 'panel-5')['data']['spec']['queries'][0]['spec']['query']['spec']
        query['expr'] = 'last_over_time(x[$__interval]) - first_over_time(x[$__interval])'
        defaults(doc, 'panel-5')['min'] = 0
        self.assert_finding(doc, 'standard', 'change over time')

    def test_rejects_time_series_without_min_zero(self):
        doc = valid()
        del defaults(doc, 'panel-2')['min']
        self.assert_finding(doc, 'standard', 'min: 0')

    def test_rejects_area_stat_without_min_zero(self):
        doc = valid()
        element(doc, 'panel-1')['vizConfig']['spec']['options']['graphMode'] = 'area'
        self.assert_finding(doc, 'standard', 'min: 0')

    def test_rejects_missing_tags(self):
        doc = valid()
        del doc['spec']['tags']
        self.assert_finding(doc, 'standard', 'no tags')

    def test_rejects_empty_tags(self):
        doc = valid()
        doc['spec']['tags'] = []
        self.assert_finding(doc, 'standard', 'no tags')

    def test_rejects_uppercase_tag(self):
        doc = valid()
        doc['spec']['tags'] = ['Queue']
        self.assert_finding(doc, 'standard', 'Queue')

    def test_rejects_datasource_type_tag(self):
        doc = valid()
        doc['spec']['tags'] = ['queue', 'prometheus']
        self.assert_finding(doc, 'standard', 'prometheus')

    def test_rejects_timezone_other_than_browser(self):
        doc = valid()
        doc['spec']['timeSettings']['timezone'] = 'utc'
        self.assert_finding(doc, 'standard', 'timezone')


class Prose(RuleCase):
    def with_description(self, text: str) -> dict:
        doc = valid()
        element(doc, 'panel-2')['description'] = text
        return doc

    BASE = 'Jobs each worker finished per second, averaged over five minutes. A gap means that worker sent no metrics'

    def test_rejects_em_dash(self):
        self.assert_finding(
            self.with_description(self.BASE + ' \u2014 or none.'), 'prose', 'em dash'
        )

    def test_rejects_semicolon(self):
        self.assert_finding(self.with_description(self.BASE + '; or none.'), 'prose', 'semicolon')

    def test_rejects_curly_quote(self):
        self.assert_finding(
            self.with_description(self.BASE + ' \u201cat all\u201d.'), 'prose', 'curly quote'
        )

    def test_rejects_arrow(self):
        self.assert_finding(self.with_description(self.BASE + ' \u2192 none.'), 'prose', 'arrow')

    def test_rejects_negative_contraction(self):
        self.assert_finding(
            self.with_description(self.BASE + ". It doesn't reset."), 'prose', 'contraction'
        )

    def test_rejects_all_caps_word(self):
        self.assert_finding(self.with_description(self.BASE + ', NEVER more.'), 'prose', 'NEVER')

    def test_accepts_allowlisted_acronym(self):
        self.assert_clean(self.with_description(self.BASE + ' over HTTP.'))

    def test_rejects_bracketed_aside(self):
        self.assert_finding(self.with_description(self.BASE + ' (or none).'), 'prose', '(or none)')

    def test_accepts_allowlisted_percentile(self):
        self.assert_clean(self.with_description(self.BASE + ' at the median (p50).'))

    def test_accepts_a_markdown_link(self):
        self.assert_clean(
            self.with_description(self.BASE + ', see [the docs](https://example.com/a).')
        )

    def test_rejects_sentence_over_35_words(self):
        long = ' '.join(['word'] * 36) + '.'
        self.assert_finding(
            self.with_description('Short first one. ' + long), 'prose', 'over 35 words'
        )

    def test_ignores_text_in_backticks(self):
        self.assert_clean(self.with_description(self.BASE + ' from `a; b \u2014 c`.'))

    def test_sweeps_mapping_text(self):
        doc = valid()
        defaults(doc, 'panel-1')['mappings'] = [
            {'type': 'value', 'options': {'0': {'text': 'Idle; empty'}}}
        ]
        self.assert_finding(doc, 'prose', 'semicolon')

    def test_sweeps_column_headers(self):
        doc = valid()
        options = element(doc, 'panel-4')['data']['spec']['transformations'][0]['spec']['options']
        options['renameByName']['version'] = 'Version \u2014 build'
        self.assert_finding(doc, 'prose', 'em dash')

    def test_sweeps_variable_labels(self):
        doc = valid()
        variable(doc, 'worker')['spec']['label'] = 'Worker \u2192 host'
        self.assert_finding(doc, 'prose', 'arrow')

    def test_sweeps_row_titles(self):
        doc = valid()
        rows(doc)[0]['spec']['title'] = 'Queue \u2014 live'
        self.assert_finding(doc, 'prose', 'em dash')


@unittest.skipUnless(CUE and SCHEMAS, 'DASHBOARD_CHECK_CUE and DASHBOARD_CHECK_SCHEMAS not set')
class StrictVet(unittest.TestCase):
    def schema_args(self) -> list[str]:
        args = []
        for path in sorted(pathlib.Path(SCHEMAS).glob('schema-*.cue')):
            args += ['--schema', f'{path.stem.removeprefix("schema-")}={path}']
        self.assertGreaterEqual(len(args), 2, f'expected a schema in {SCHEMAS}')
        return args

    def check(self, doc: dict) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'grafana-dashboard.json'
            path.write_text(json.dumps(doc, indent=2))
            return run_cli('--cue', CUE, *self.schema_args(), str(path))

    def test_valid_fixture_vets_against_every_schema(self):
        code, out = self.check(valid())
        self.assertEqual(code, 0, out)

    def test_rejects_an_element_of_unknown_kind(self):
        doc = valid()
        doc['spec']['elements']['panel-1']['kind'] = 'Garbage'
        code, out = self.check(doc)
        self.assertEqual(code, 1, out)
        self.assertIn('[cue]', out)
        self.assertIn('Garbage', out)

    def test_malformed_members_are_cue_findings_and_skip_the_spec_rules(self):
        for member in ('row', 'query'):
            with self.subTest(member=member):
                doc = valid()
                if member == 'row':
                    rows(doc)[0] = 'row'
                else:
                    element(doc, 'panel-2')['data']['spec']['queries'][0] = 7
                code, out = self.check(doc)
                self.assertEqual(code, 1, out)
                self.assertIn('[cue]', out)
                self.assertNotIn('[references]', out)
                self.assertNotIn('[datasource]', out)

    def test_stray_conditional_rendering_passes_the_vet_and_fails_the_layout_rule(self):
        for place, holder in STRAY_CONDITIONAL_PLACES.items():
            with self.subTest(place=place):
                doc = valid()
                holder(doc)['conditionalRendering'] = conditional_group()
                code, out = self.check(doc)
                self.assertEqual(code, 1, out)
                self.assertNotIn('[cue]', out)
                self.assertIn('[layout] conditionalRendering sits outside', out)

    def test_names_the_schema_and_the_file_location(self):
        doc = valid()
        defaults(doc, 'panel-1')['thresholds']['mode'] = 'relative'
        code, out = self.check(doc)
        self.assertEqual(code, 1, out)
        cue_lines = [line for line in out.splitlines() if '[cue]' in line]
        self.assertTrue(cue_lines, out)
        self.assertIn('v13.2.3', cue_lines[0])
        self.assertIn('grafana-dashboard.json:', cue_lines[0])


if __name__ == '__main__':
    unittest.main()
