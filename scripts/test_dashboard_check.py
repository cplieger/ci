"""testdata/dashboard-check/valid.json must pass every check: each rule test mutates a copy.

The CUE tests run only when DASHBOARD_CHECK_CUE (the cue binary),
DASHBOARD_CHECK_SCHEMAS (the directory install.sh filled) and
DASHBOARD_CHECK_PROMQL (the promql-check binary) are set; the PromQL tests
need only DASHBOARD_CHECK_PROMQL.
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
    ONE_DASHBOARD,
    TWO_DASHBOARDS,
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
PROMQL = os.environ.get('DASHBOARD_CHECK_PROMQL', '')


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
    def test_rejects_literal_datasource_uid(self):
        doc = valid()
        element(doc, 'panel-2')['data']['spec']['queries'][0]['spec']['query']['datasource'] = {
            'name': 'mimir'
        }
        self.assert_finding(doc, 'datasource', 'mimir')

    def test_rejects_query_without_datasource(self):
        doc = valid()
        del element(doc, 'panel-2')['data']['spec']['queries'][0]['spec']['query']['datasource']
        self.assert_finding(doc, 'datasource', 'no datasource')

    def test_rejects_variable_with_literal_datasource(self):
        doc = valid()
        variable(doc, 'worker')['spec']['query']['datasource'] = {'name': 'P1809F7CD0C75ACF3'}
        self.assert_finding(doc, 'datasource', 'P1809F7CD0C75ACF3')

    def test_accepts_a_panel_reading_the_dashboard_datasource(self):
        doc = valid()
        element(doc, 'panel-2')['data']['spec']['queries'][0]['spec']['query'] = {
            'kind': 'DataQuery',
            'group': 'datasource',
            'version': 'v0',
            'datasource': {'name': '-- Dashboard --'},
            'spec': {'panelId': 1, 'withTransforms': False},
        }
        self.assert_clean(doc)

    def test_rejects_the_dashboard_datasource_name_under_another_group(self):
        doc = valid()
        element(doc, 'panel-2')['data']['spec']['queries'][0]['spec']['query']['datasource'] = {
            'name': '-- Dashboard --'
        }
        self.assert_finding(doc, 'datasource', '-- Dashboard --')

    def test_rejects_a_literal_uid_under_the_dashboard_group(self):
        doc = valid()
        query = element(doc, 'panel-2')['data']['spec']['queries'][0]['spec']['query']
        query['group'], query['datasource'] = 'datasource', {'name': 'mimir'}
        self.assert_finding(doc, 'datasource', 'mimir')

    def test_rejects_a_variable_reading_the_dashboard_datasource(self):
        doc = valid()
        variable(doc, 'worker')['spec']['query']['datasource'] = {'name': '-- Dashboard --'}
        variable(doc, 'worker')['spec']['query']['group'] = 'datasource'
        self.assert_finding(doc, 'datasource', '-- Dashboard --')

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

    def test_overlapping_items_fail(self):
        doc = valid()
        grid_items(doc)[2]['spec'].update(x=6, y=4)
        grid_items(doc)[1]['spec']['height'] = 9
        self.assert_finding(
            doc,
            'layout',
            'panel-2 "Jobs finished" (x=0, y=4, width=12, height=9) and '
            'panel-3 "Jobs failed" (x=6, y=4, width=12, height=8) cover the same grid cells',
        )

    def test_each_overlapping_pair_is_reported_once(self):
        doc = valid()
        grid_items(doc)[0]['spec']['height'] = 5
        found = [f for f in self.findings(doc) if 'cover the same grid cells' in f.message]
        self.assertEqual(len(found), 2, [str(f) for f in found])
        self.assertIn('panel-1', found[0].message)
        self.assertIn('panel-2', found[0].message)
        self.assertIn('panel-3', found[1].message)

    def test_touching_items_pass(self):
        doc = valid()
        self.assertEqual(
            [(i['spec']['x'], i['spec']['y']) for i in grid_items(doc)], [(0, 0), (0, 4), (12, 4)]
        )
        self.assert_clean(doc)


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

    def test_at_start_subtraction_is_a_change(self):
        doc = valid()
        query = element(doc, 'panel-5')['data']['spec']['queries'][0]['spec']['query']['spec']
        query['expr'] = 'sum(x{a="b"} - (x{a="b"} @ start()))'
        defaults(doc, 'panel-5')['min'] = 0
        self.assert_finding(doc, 'standard', 'change over time')

    def test_at_start_without_subtraction_is_not_a_change(self):
        doc = valid()
        query = element(doc, 'panel-5')['data']['spec']['queries'][0]['spec']['query']['spec']
        query['expr'] = 'x @ start()'
        self.assert_finding(doc, 'standard', 'min: 0')

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


def with_instance(doc: dict) -> dict:
    """Adds a single-select instance variable, which Q2 matches every instance matcher against,
    and scopes every other query to it, as an app dashboard does."""
    scope = 'instance="$instance", worker=~"$worker"'
    for name in doc['spec']['elements']:
        query = panel_query(doc, name)['spec']
        query['expr'] = query['expr'].replace('worker=~"$worker"', scope)
    variable(doc, 'worker')['spec']['query']['spec']['query'] = (
        'label_values(worker_info{instance="$instance"}, worker)'
    )
    var = copy.deepcopy(variable(doc, 'worker'))
    var['spec'].update(
        name='instance',
        label='Instance',
        description='The instance to show.',
        multi=False,
        includeAll=False,
        definition='label_values(worker_info, instance)',
    )
    var['spec'].pop('allValue')
    var['spec']['query']['spec']['query'] = 'label_values(worker_info, instance)'
    doc['spec']['variables'].append(var)
    return doc


def interval_variable(name: str, **spec: object) -> dict:
    return {
        'kind': 'IntervalVariable',
        'spec': {
            'name': name,
            'current': {'text': '1m', 'value': '1m'},
            'refresh': 'onTimeRangeChanged',
            'label': 'Window',
            'description': 'The rate window.',
            'hide': 'dontHide',
            'skipUrlSync': False,
        }
        | spec,
    }


def adhoc_variable(group: str = 'prometheus', **spec: object) -> dict:
    return {
        'kind': 'AdhocVariable',
        'group': group,
        'datasource': {'name': '${datasource}'},
        'spec': {
            'name': 'filters',
            'label': 'Filters',
            'hide': 'dontHide',
            'skipUrlSync': False,
            'allowCustomValue': True,
            'defaultKeys': [],
            'baseFilters': [],
            'filters': [],
        }
        | spec,
    }


def panel_query(doc: dict, name: str) -> dict:
    return element(doc, name)['data']['spec']['queries'][0]['spec']['query']


def annotation(expr: str) -> dict:
    return {
        'kind': 'AnnotationQuery',
        'spec': {
            'query': {
                'kind': 'DataQuery',
                'group': 'prometheus',
                'version': 'v0',
                'datasource': {'name': '${datasource}'},
                'spec': {'expr': expr},
            },
            'enable': True,
            'hide': False,
            'iconColor': 'red',
            'name': 'Restarts',
        },
    }


class QueryExtraction(RuleCase):
    """What the checker hands promql-check, read without the binary."""

    def collected(self, doc: dict) -> dc.QueryInput:
        seen = []
        dc.check(doc, None, None, lambda queries: seen.append(queries) or [])
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_reads_every_prometheus_query_and_variable(self):
        got = self.collected(valid())
        self.assertEqual(
            got.queries,
            [
                {
                    'where': 'element panel-1 "Queue depth" query 0',
                    'kind': 'panel',
                    'expr': 'sum(queue_depth{worker=~"$worker"})',
                },
                {
                    'where': 'element panel-2 "Jobs finished" query 0',
                    'kind': 'panel',
                    'expr': 'sum by (worker) (rate(jobs_finished_total{worker=~"$worker"}[5m]))',
                },
                {
                    'where': 'element panel-3 "Jobs failed" query 0',
                    'kind': 'panel',
                    'expr': 'sum by (worker) (rate(jobs_failed_total{worker=~"$worker"}[5m]))',
                },
                {
                    'where': 'element panel-4 "Workers" query 0',
                    'kind': 'panel',
                    'expr': 'max by (worker, version) (worker_info{worker=~"$worker"})',
                },
                {
                    'where': 'element panel-5 "Queue change" query 0',
                    'kind': 'panel',
                    'expr': 'delta(queue_depth{worker=~"$worker"}[$__rate_interval])',
                },
                {
                    'where': 'spec.variables variable worker',
                    'kind': 'variable',
                    'expr': 'label_values(worker_info, worker)',
                    'variable': 'worker',
                },
            ],
        )
        self.assertEqual(
            got.variables,
            [
                {
                    'name': 'datasource',
                    'kind': 'DatasourceVariable',
                    'multi': False,
                    'includeAll': False,
                },
                {'name': 'worker', 'kind': 'QueryVariable', 'multi': True, 'includeAll': True},
                {'name': 'window', 'kind': 'TextVariable', 'multi': False, 'includeAll': False},
            ],
        )

    def test_multi_and_include_all_are_sent_only_when_true(self):
        doc = valid()
        variable(doc, 'worker')['spec'].update(multi='yes', includeAll=1)
        worker = [v for v in self.collected(doc).variables if v['name'] == 'worker']
        self.assertEqual(
            worker,
            [{'name': 'worker', 'kind': 'QueryVariable', 'multi': False, 'includeAll': False}],
        )

    def test_an_interval_variable_sends_its_choices(self):
        doc = valid()
        doc['spec']['variables'].append(
            interval_variable(
                'iv',
                query="1m,'15s', 1m, '$__auto_interval_iv'",
                auto=True,
                auto_min='10s',
                auto_count=30,
            )
        )
        got = [v for v in self.collected(doc).variables if v['name'] == 'iv']
        self.assertEqual(
            got,
            [
                {
                    'name': 'iv',
                    'kind': 'IntervalVariable',
                    'multi': False,
                    'includeAll': False,
                    'intervals': ['1m', '15s'],
                    'auto': True,
                    'autoMin': '10s',
                    'autoCount': 30,
                }
            ],
        )

    def test_interval_choices_follow_grafanas_reading(self):
        cases = {
            'query': ({'query': '"1m30s",10m'}, ['1m30s', '10m']),
            'options when the query is empty': (
                {'query': '', 'options': [{'value': '', 'text': '5m'}, {'value': '1h'}]},
                ['5m', '1h'],
            ),
            'defaults with neither': ({}, list(dc.DEFAULT_INTERVALS)),
            'defaults when the query holds no interval': (
                {'query': ', ,'},
                list(dc.DEFAULT_INTERVALS),
            ),
        }
        for label, (spec, want) in cases.items():
            with self.subTest(case=label):
                got = dc.interval_choices(spec)
                self.assertEqual(got['intervals'], want)
                self.assertEqual((got['auto'], got['autoMin'], got['autoCount']), (False, '', 0))
        odd = dc.interval_choices({'auto': 'yes', 'auto_min': 10, 'auto_count': True})
        self.assertEqual((odd['auto'], odd['autoMin'], odd['autoCount']), (False, '', 0))

    def test_a_panel_query_sends_its_instant_and_range_flags(self):
        doc = valid()
        panel_query(doc, 'panel-1')['spec'].update(instant=True, range=False)
        panel_query(doc, 'panel-2')['spec'].update(instant=True, range=True)
        panel_query(doc, 'panel-3')['spec'].update(instant='yes')
        flags = [
            {k: q[k] for k in ('instant', 'range') if k in q}
            for q in self.collected(doc).queries[:3]
        ]
        self.assertEqual(flags, [{'instant': True}, {'instant': True, 'range': True}, {}])

    def test_an_adhoc_filter_reaches_the_input(self):
        doc = valid()
        doc['spec']['variables'].append(
            adhoc_variable(
                filters=[
                    {'key': 'job', 'operator': '=', 'value': 'x'},
                    {'key': 'instance', 'operator': '=|', 'value': 'a', 'values': ['a.b', 'c']},
                    {'key': 'job', 'operator': 'groupBy', 'value': 'y'},
                    {'key': 'job', 'operator': '=', 'value': ''},
                    {'key': 'job', 'operator': '=', 'value': 'y', 'nonApplicable': True},
                    {'key': 'a.b', 'operator': '=~', 'value': "o'k"},
                ],
            )
        )
        got = [q for q in self.collected(doc).queries if q['kind'] == 'filter']
        at = 'spec.variables variable filters'
        self.assertEqual(
            got,
            [
                {'where': f'{at} filters[0]', 'kind': 'filter', 'expr': 'job="x"'},
                {'where': f'{at} filters[1]', 'kind': 'filter', 'expr': 'instance=~"a.b|c"'},
                {'where': f'{at} filters[5]', 'kind': 'filter', 'expr': '"a.b"=~"o\\\\\'k"'},
            ],
        )

    def test_a_base_filter_sends_no_filter_query(self):
        doc = valid()
        doc['spec']['variables'].append(
            adhoc_variable(baseFilters=[{'key': 'job', 'operator': '=', 'value': 'x'}])
        )
        self.assertEqual([q for q in self.collected(doc).queries if q['kind'] == 'filter'], [])

    def test_a_custom_or_constant_variable_sends_its_values(self):
        cases = {
            'comma separated': ('CustomVariable', {'query': '1, 2.5 ,3'}, ['1', '2.5', '3']),
            'escaped comma': ('CustomVariable', {'query': 'a\\,b,c'}, ['a,b', 'c']),
            'key and value': ('CustomVariable', {'query': 'one : 1,two : 2'}, ['1', '2']),
            'a colon with no spaces is text': ('CustomVariable', {'query': 'a:1'}, ['a:1']),
            'json': (
                'CustomVariable',
                {
                    'query': '[{"text": "a", "value": 5}, {"value": " 6 "}, 7]',
                    'valuesFormat': 'json',
                },
                ['5', '6'],
            ),
            'bad json': ('CustomVariable', {'query': '[', 'valuesFormat': 'json'}, []),
            'json that is no array': (
                'CustomVariable',
                {'query': '{}', 'valuesFormat': 'json'},
                [],
            ),
            'empty': ('CustomVariable', {'query': ''}, []),
            'no query': ('CustomVariable', {}, []),
            'constant': ('ConstantVariable', {'query': '5m'}, ['5m']),
        }
        for label, (kind, spec, want) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(dc.variable_values(kind, spec), want)
        got = dc.QueryInput()
        got.variable('v', 'CustomVariable', {'name': 'n', 'query': '1,2'})
        got.variable('v', 'ConstantVariable', {'name': 'c', 'query': '5m'})
        got.variable('v', 'TextVariable', {'name': 't', 'query': 'x'})
        self.assertEqual(
            [(v['name'], v.get('values')) for v in got.variables],
            [('n', ['1', '2']), ('c', ['5m']), ('t', None)],
        )

    def test_a_loki_adhoc_filter_is_skipped(self):
        doc = valid()
        doc['spec']['variables'].append(
            adhoc_variable('loki', filters=[{'key': 'job', 'operator': '=', 'value': 'x'}])
        )
        self.assertEqual([q for q in self.collected(doc).queries if q['kind'] == 'filter'], [])

    def test_loki_queries_are_skipped(self):
        doc = valid()
        panel_query(doc, 'panel-2').update(group='loki', spec={'expr': 'rate({app="x"}[1m])'})
        wheres = [q['where'] for q in self.collected(doc).queries]
        self.assertNotIn('element panel-2 "Jobs finished" query 0', wheres)
        self.assertIn('element panel-3 "Jobs failed" query 0', wheres)

    def test_a_variable_sends_its_query_not_its_definition(self):
        doc = valid()
        variable(doc, 'worker')['spec']['definition'] = 'not what Grafana runs'
        got = [q for q in self.collected(doc).queries if q['kind'] == 'variable']
        self.assertEqual([q['expr'] for q in got], ['label_values(worker_info, worker)'])

    def test_a_variable_stored_as_a_legacy_string_sends_that_string(self):
        doc = valid()
        spec = variable(doc, 'worker')['spec']['query']['spec']
        spec['__legacyStringValue'] = 'query_result(rate(x[$__interval]))'
        got = [q['expr'] for q in self.collected(doc).queries if q['kind'] == 'variable']
        self.assertEqual(got, ['query_result(rate(x[$__interval]))'])

    def test_a_null_legacy_string_sends_an_empty_query(self):
        doc = valid()
        variable(doc, 'worker')['spec']['query']['spec']['__legacyStringValue'] = None
        got = [q['expr'] for q in self.collected(doc).queries if q['kind'] == 'variable']
        self.assertEqual(got, [''])

    def test_reads_row_and_tab_variables(self):
        doc = valid()
        row_var = copy.deepcopy(variable(doc, 'worker'))
        row_var['spec']['name'] = 'row_worker'
        rows(doc)[1]['spec']['variables'] = [row_var]
        tab_var = copy.deepcopy(variable(doc, 'worker'))
        tab_var['spec']['name'] = 'tab_worker'
        doc['spec']['layout'] = tabs_of(('Overview', doc['spec']['layout']))
        doc['spec']['layout']['spec']['tabs'][0]['spec']['variables'] = [tab_var]
        got = self.collected(doc)
        names = [v['name'] for v in got.variables]
        self.assertIn('row_worker', names)
        self.assertIn('tab_worker', names)
        variable_wheres = [q['where'] for q in got.queries if q['kind'] == 'variable']
        self.assertTrue(any(w.endswith('variable row_worker') for w in variable_wheres))
        self.assertTrue(any(w.endswith('variable tab_worker') for w in variable_wheres))

    def test_reads_annotation_queries_but_not_the_built_in(self):
        doc = valid()
        doc['spec']['annotations'].append(annotation('changes(up[5m]) > 0'))
        got = [q for q in self.collected(doc).queries if q['kind'] == 'annotation']
        self.assertEqual(
            got,
            [{'where': 'spec.annotations[1]', 'kind': 'annotation', 'expr': 'changes(up[5m]) > 0'}],
        )

    def test_a_query_text_that_is_not_a_string_is_sent_empty(self):
        doc = valid()
        panel_query(doc, 'panel-1')['spec']['expr'] = 7
        got = self.collected(doc).queries[0]
        self.assertEqual(got['expr'], '')

    def test_the_query_check_runs_only_on_a_spec_the_vet_accepts(self):
        seen = []
        dc.check(
            valid(),
            None,
            lambda: [dc.Finding('spec', 'cue', 'bad', 'fix')],
            lambda queries: seen.append(queries) or [],
        )
        self.assertEqual(seen, [])


class AppDashboard(unittest.TestCase):
    def test_app_dashboard_by_path(self):
        cases = {
            'grafana-dashboard.json': True,
            'grafana-dashboard-messages.json': True,
            'grafana-dashboard-.json': False,
            'grafana-dashboard.json.bak': False,
            'my-grafana-dashboard.json': False,
            'sub/grafana-dashboard.json': False,
            'grafana-dashboards/live-operations.json': False,
            '/abs/grafana-dashboard.json': False,
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertIs(dc.is_app_dashboard(pathlib.Path(path)), want)


class QueryCheckRun(unittest.TestCase):
    """QueryCheck fails the file when promql-check cannot answer."""

    def fake(self, tmp: str, body: str) -> str:
        path = pathlib.Path(tmp) / 'promql-check'
        path.write_text(f'#!/bin/sh\n{body}\n')
        path.chmod(0o755)
        return str(path)

    def test_a_check_that_cannot_answer_fails_the_file(self):
        cases = {
            'missing binary': None,
            'non-zero exit': 'echo \'{"checked": 0, "findings": []}\'; echo broken >&2; exit 2',
            'not JSON': 'echo nope',
            'no findings key': 'echo \'{"checked": 1}\'',
        }
        for label, body in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                binary = '/nonexistent/promql-check' if body is None else self.fake(tmp, body)
                found, _ = dc.check(valid(), None, None, dc.QueryCheck(binary, app=True))
                self.assertEqual([f.rule for f in found], ['promql'], [str(f) for f in found])
                self.assertIn('the PromQL query check did not run', found[0].message)

    def test_findings_are_passed_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = {
                'checked': 6,
                'findings': [{'where': 'w', 'rule': 'Q1', 'message': 'm.', 'fix': 'f.'}],
            }
            binary = self.fake(tmp, f"cat >/dev/null; echo '{json.dumps(report)}'")
            query_check = dc.QueryCheck(binary, app=False)
            found, _ = dc.check(valid(), None, None, query_check)
        self.assertEqual(found, [dc.Finding('w', 'Q1', 'm.', 'f.')])
        self.assertEqual(
            query_check.summary(), '6 Prometheus queries parsed, not an app dashboard: Q1 only'
        )


@unittest.skipUnless(PROMQL, 'DASHBOARD_CHECK_PROMQL not set')
class PromQL(unittest.TestCase):
    """The checker and promql-check together, on the fixture."""

    def findings(self, doc: dict, app: bool = True) -> list:
        found, _ = dc.check(doc, None, None, dc.QueryCheck(PROMQL, app=app))
        return found

    def test_valid_fixture_passes(self):
        query_check = dc.QueryCheck(PROMQL, app=True)
        found, _ = dc.check(valid(), None, None, query_check)
        self.assertEqual([str(f) for f in found], [])
        self.assertEqual(query_check.checked, 6)

    def test_each_rule_names_the_panel_and_quotes_the_query(self):
        doc = with_instance(valid())
        expr = 'sum(increase(up{instance=~"$instance|", job="x"}[${__interval_ms}ms]))'
        panel_query(doc, 'panel-2')['spec']['expr'] = expr
        found = self.findings(doc)
        self.assertEqual([f.rule for f in found], ['Q1', 'Q2', 'Q3'], [str(f) for f in found])
        for f in found:
            with self.subTest(rule=f.rule):
                self.assertEqual(f.where, 'element panel-2 "Jobs finished" query 0')
                self.assertIn(f'The query: {json.dumps(expr)}.', f.message)
                self.assertTrue(f.fix)

    def test_parse_error_names_the_panel(self):
        doc = valid()
        panel_query(doc, 'panel-3')['spec']['expr'] = 'sum(rate(x[5m])'
        found = self.findings(doc)
        self.assertEqual([f.rule for f in found], ['promql'], [str(f) for f in found])
        self.assertEqual(found[0].where, 'element panel-3 "Jobs failed" query 0')
        self.assertIn('unclosed left parenthesis', found[0].message)

    def test_loki_queries_are_skipped(self):
        doc = valid()
        panel_query(doc, 'panel-2').update(
            group='loki', spec={'expr': 'sum(rate({app="x", job="y"} |= "a|b" [$__interval]))'}
        )
        self.assertEqual([str(f) for f in self.findings(doc)], [])

    def test_an_adhoc_filter_is_held_to_q2_and_q3(self):
        for label, f, rule in (
            ('job', {'key': 'job', 'operator': '=', 'value': 'x'}, 'Q3'),
            ('instance regex', {'key': 'instance', 'operator': '=~', 'value': 'a|b'}, 'Q2'),
        ):
            with self.subTest(filter=label):
                doc = with_instance(valid())
                doc['spec']['variables'].append(adhoc_variable(filters=[f]))
                found = self.findings(doc)
                self.assertEqual(
                    [(f.where, f.rule) for f in found],
                    [('spec.variables variable filters filters[0]', rule)],
                    [str(f) for f in found],
                )

    def test_an_instant_query_refuses_a_range_window(self):
        doc = with_instance(valid())
        spec = panel_query(doc, 'panel-1')['spec']
        spec.update(
            expr='sum(max_over_time(queue_depth{instance="$instance"}[range()]))', instant=True
        )
        found = self.findings(doc)
        self.assertEqual([f.rule for f in found], ['promql'], [str(f) for f in found])
        self.assertIn('as an instant query, where step() and range() are 0', found[0].message)

    def test_variable_queries_are_checked(self):
        doc = with_instance(valid())
        variable(doc, 'instance')['spec']['query']['spec']['query'] = (
            'label_values(worker_info{job="x"}, instance)'
        )
        found = self.findings(doc)
        # Q2: a matcher beside the metric name narrows the instance picker.
        self.assertEqual([f.rule for f in found], ['Q2', 'Q3'], [str(f) for f in found])
        self.assertIn('the instance variable can hide instances', found[0].message)
        for f in found:
            self.assertEqual(f.where, 'spec.variables variable instance')

    def test_a_multi_value_instance_variable_fails_q2(self):
        for field in ('multi', 'includeAll'):
            with self.subTest(field=field):
                doc = with_instance(valid())
                variable(doc, 'instance')['spec'][field] = True
                found = self.findings(doc)
                self.assertEqual(len(found), 6, [str(f) for f in found])
                for f in found:
                    self.assertEqual(f.rule, 'Q2')
                    self.assertIn('multi-select or includes All', f.message)

    def test_a_formatted_instance_reference_fails_q2(self):
        doc = with_instance(valid())
        panel_query(doc, 'panel-1')['spec']['expr'] = 'sum(up{instance="${instance:regex}"})'
        found = self.findings(doc)
        self.assertEqual([f.rule for f in found], ['Q2'], [str(f) for f in found])
        self.assertIn('regex-escaped', found[0].message)

    def test_a_selector_without_the_instance_fails_q2(self):
        for where, at in (
            ('panel', 'element panel-3 "Jobs failed" query 0'),
            ('variable', 'spec.variables variable worker'),
        ):
            with self.subTest(where=where):
                doc = with_instance(valid())
                if where == 'panel':
                    panel_query(doc, 'panel-3')['spec']['expr'] = (
                        'sum(up{instance="$instance"}) or sum(up)'
                    )
                else:
                    variable(doc, 'worker')['spec']['query']['spec']['query'] = (
                        'label_values(worker_info, worker)'
                    )
                found = self.findings(doc)
                self.assertEqual([(f.where, f.rule) for f in found], [(at, 'Q2')])
                self.assertIn(
                    "has no instance matcher, so it reads every instance's", found[0].message
                )

    def test_the_instance_variable_query_needs_no_instance_matcher(self):
        self.assertEqual([str(f) for f in self.findings(with_instance(valid()))], [])

    def test_a_legacy_stored_variable_query_is_checked(self):
        doc = valid()
        spec = variable(doc, 'worker')['spec']['query']['spec']
        spec['__legacyStringValue'] = 'query_result(sum(rate(x[$__interval])))'
        found = self.findings(doc)
        self.assertEqual(
            [(f.where, f.rule) for f in found], [('spec.variables variable worker', 'Q1')]
        )

    def test_an_interval_variable_reaches_q1_at_its_shortest_choice(self):
        for label, spec, want in (
            (
                'auto',
                {'query': '1m,10m', 'auto': True, 'auto_min': '10s', 'auto_count': 30},
                ['Q1'],
            ),
            ('a 15s option', {'query': '15s,1m'}, ['Q1']),
            ('options from 1m, auto off', {'query': '1m,10m'}, []),
        ):
            with self.subTest(case=label):
                doc = valid()
                doc['spec']['variables'].append(interval_variable('iv', **spec))
                panel_query(doc, 'panel-2')['spec']['expr'] = 'sum(rate(jobs_finished_total[$iv]))'
                self.assertEqual([f.rule for f in self.findings(doc)], want)

    def test_variable_definition_is_not_read(self):
        doc = valid()
        variable(doc, 'worker')['spec']['definition'] = 'rate(x{job="y"}[$__interval]'
        self.assertEqual([str(f) for f in self.findings(doc)], [])

    def test_annotation_queries_are_checked(self):
        doc = valid()
        doc['spec']['annotations'].append(annotation('irate(x[$__interval]) > 0'))
        found = self.findings(doc)
        self.assertEqual([(f.where, f.rule) for f in found], [('spec.annotations[1]', 'Q1')])

    def test_not_an_app_dashboard_gets_q1_only(self):
        doc = valid()
        panel_query(doc, 'panel-1')['spec']['expr'] = 'sum(up{instance=~".+", hostname="h"})'
        panel_query(doc, 'panel-2')['spec']['expr'] = 'sum(rate(x[$__interval]))'
        self.assertEqual([f.rule for f in self.findings(doc, app=False)], ['Q1'])
        self.assertEqual([f.rule for f in self.findings(doc, app=True)], ['Q2', 'Q3', 'Q1'])


@unittest.skipUnless(
    CUE and SCHEMAS and PROMQL,
    'DASHBOARD_CHECK_CUE, DASHBOARD_CHECK_SCHEMAS and DASHBOARD_CHECK_PROMQL not set',
)
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
            return run_cli('--cue', CUE, '--promql', PROMQL, *self.schema_args(), str(path))

    def test_valid_fixture_vets_against_every_schema(self):
        code, out = self.check(valid())
        self.assertEqual(code, 0, out)

    def test_each_bundled_dashboard_fixture_vets(self):
        for repo, count in ((ONE_DASHBOARD, 1), (TWO_DASHBOARDS, 2)):
            paths = sorted(repo.glob('grafana-dashboard*.json'))
            self.assertEqual(len(paths), count, repo)
            for path in paths:
                with self.subTest(file=f'{repo.name}/{path.name}'):
                    code, out = run_cli(
                        '--cue', CUE, '--promql', PROMQL, *self.schema_args(), str(path)
                    )
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

    def assert_rejected_at(self, doc: dict, path: str) -> None:
        code, out = self.check(doc)
        self.assertEqual(code, 1, out)
        cue_lines = [line for line in out.splitlines() if '[cue]' in line]
        self.assertTrue(cue_lines, out)
        self.assertIn(path, cue_lines[0])

    def test_rejects_conditional_rendering_where_grafana_ignores_it(self):
        grid = 'spec.layout.spec.rows.0.spec.layout.spec'
        places = {
            'panel spec': ('spec.elements."panel-1".spec', lambda doc: element(doc, 'panel-1')),
            'dashboard spec': ('spec', lambda doc: doc['spec']),
            'grid item': (f'{grid}.items.0.spec', lambda doc: grid_items(doc)[0]['spec']),
            'grid layout spec': (grid, lambda doc: rows(doc)[0]['spec']['layout']['spec']),
        }
        for place, (path, holder) in places.items():
            with self.subTest(place=place):
                doc = valid()
                holder(doc)['conditionalRendering'] = conditional_group()
                self.assert_rejected_at(doc, f'{path}.conditionalRendering: field not allowed')

    def test_accepts_conditional_rendering_on_an_auto_grid_item_a_row_or_a_tab(self):
        def on_tab(doc: dict) -> dict:
            doc['spec']['layout'] = tabs_of(('Overview', doc['spec']['layout']))
            return doc['spec']['layout']['spec']['tabs'][0]['spec']

        places = {
            'auto-grid item': lambda doc: rows(doc)[1]['spec']['layout']['spec']['items'][0][
                'spec'
            ],
            'row': lambda doc: rows(doc)[0]['spec'],
            'tab': on_tab,
        }
        for place, holder in places.items():
            with self.subTest(place=place):
                doc = valid()
                holder(doc)['conditionalRendering'] = conditional_group()
                code, out = self.check(doc)
                self.assertEqual(code, 0, out)

    def test_rejects_auto_grid_fields_only_grafana_main_has(self):
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
                self.assert_rejected_at(
                    doc, f'spec.layout.spec.rows.1.spec.layout.spec.{field}: field not allowed'
                )

    def test_rejects_a_datasource_that_is_not_a_bare_name(self):
        query_path = 'spec.elements."panel-2".spec.data.spec.queries.0.spec.query.datasource'
        refs = {
            'a string': ('${datasource}', query_path),
            'a uid beside the name': (
                {'name': '${datasource}', 'uid': 'hard-coded-uid'},
                f'{query_path}.uid: field not allowed',
            ),
        }
        for label, (ref, path) in refs.items():
            with self.subTest(ref=label):
                doc = valid()
                element(doc, 'panel-2')['data']['spec']['queries'][0]['spec']['query'][
                    'datasource'
                ] = ref
                self.assert_rejected_at(doc, path)

    def test_rejects_a_key_the_schema_does_not_define_at_any_depth(self):
        def misspell_field_config(doc: dict) -> None:
            viz = element(doc, 'panel-1')['vizConfig']['spec']
            viz['fieldconfig'] = viz.pop('fieldConfig')

        def misspell_in_a_tab(doc: dict) -> None:
            grid_items(doc)[0]['spec']['widht'] = 4
            doc['spec']['layout'] = tabs_of(('Overview', doc['spec']['layout']))

        in_tab = 'spec.layout.spec.tabs.0.spec.layout.spec.rows.0.spec.layout.spec.items.0'

        cases = {
            'spec.bogusField': lambda doc: doc['spec'].update(bogusField=1),
            'spec.elements."panel-1".spec.vizConfig.spec.fieldconfig': misspell_field_config,
            'spec.layout.spec.rows.0.spec.titel': lambda doc: rows(doc)[0]['spec'].update(
                titel='Queue'
            ),
            'spec.layout.spec.rows.0.spec.layout.spec.items.0.spec.widht': lambda doc: grid_items(
                doc
            )[0]['spec'].update(widht=4),
            f'{in_tab}.spec.widht': misspell_in_a_tab,
            'spec.variables.1.spec.lable': lambda doc: variable(doc, 'worker')['spec'].update(
                lable='Worker'
            ),
        }
        for path, plant in cases.items():
            with self.subTest(path=path):
                doc = valid()
                plant(doc)
                self.assert_rejected_at(doc, f'{path}: field not allowed')

        with self.subTest(path='a row key and a key in its grid item'):
            doc = valid()
            rows(doc)[0]['spec']['titel'] = 'Queue'
            grid_items(doc)[0]['spec']['widht'] = 4
            row = 'spec.layout.spec.rows.0.spec'
            for path in (f'{row}.titel', f'{row}.layout.spec.items.0.spec.widht'):
                self.assert_rejected_at(doc, f'{path}: field not allowed |')

    def test_free_form_fields_stay_open(self):
        free = {'pluginOnly': {'nested': [1, {'deeper': True}]}}
        doc = valid()
        panel = element(doc, 'panel-2')
        viz = panel['vizConfig']['spec']
        viz['options']['legend'].update(free)
        viz['fieldConfig']['defaults']['custom'].update(free)
        viz['fieldConfig']['defaults']['links'] = [free]
        viz['fieldConfig']['overrides'].append(
            {
                'matcher': {'id': 'byName', 'options': free},
                'properties': [{'id': 'custom.width', 'value': free}],
            }
        )
        panel['data']['spec']['queries'][0]['spec']['query']['spec'].update(free)
        table = element(doc, 'panel-4')['data']['spec']['transformations'][0]['spec']
        table['options'].update(free)
        doc['spec']['annotations'][0]['spec']['legacyOptions'] = free
        code, out = self.check(doc)
        self.assertEqual(code, 0, out)

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
