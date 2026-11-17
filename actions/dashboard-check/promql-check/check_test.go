package main

import (
	"slices"
	"strings"
	"testing"
	"time"
)

var dashboardVars = []variable{
	{Name: "datasource", Kind: "DatasourceVariable"},
	{Name: "instance", Kind: "QueryVariable"},
	{Name: "label", Kind: "CustomVariable"},
	{Name: "window", Kind: "IntervalVariable", Intervals: []string{"1m", "10m", "1h"}},
	{Name: "auto_window", Kind: "IntervalVariable", Intervals: []string{"1m", "10m"}, Auto: true, AutoMin: "10s", AutoCount: 30},
	{Name: "auto_long_floor", Kind: "IntervalVariable", Intervals: []string{"1m", "10m"}, Auto: true, AutoMin: "1m", AutoCount: 30},
	{Name: "fast_window", Kind: "IntervalVariable", Intervals: []string{"1m", "15s"}},
}

func run(t *testing.T, in input) report {
	t.Helper()
	out, err := check(in)
	if err != nil {
		t.Fatalf("check: %v", err)
	}
	return out
}

func rulesOf(r report) []string {
	out := []string{}
	for _, f := range r.Findings {
		out = append(out, f.Rule)
	}
	return out
}

// want is the rules a query breaks, comma-separated in the order apply reports them; "" passes.
func assertRules(t *testing.T, in input, want string) {
	t.Helper()
	r := run(t, in)
	wanted := []string{}
	if want != "" {
		wanted = strings.Split(want, ",")
	}
	if got := rulesOf(r); !slices.Equal(got, wanted) {
		t.Errorf("rules = %v, want %v; findings: %+v", got, wanted, r.Findings)
	}
}

func appQuery(kind, expr string) input {
	return input{App: true, Variables: dashboardVars, Queries: []query{{Where: "q", Kind: kind, Expr: expr}}}
}

func TestQueries(t *testing.T) {
	cases := []struct {
		name, kind, variable, expr, want string
	}{
		{name: "bracket_format", expr: `sum(rate(x_total{instance="$instance"}[[[__interval:raw]]]))`, want: "Q1"},
		{name: "bracket_ms_format", expr: `sum(rate(x_total{instance="$instance"}[[[__interval_ms:raw]]ms]))`, want: "Q1"},
		{name: "comment_quote_hides_instance", expr: "# it's here\nsum(up{instance=~\"$instance|\"})", want: "Q2"},
		{name: "comment_quote_hides_window", expr: "# don't\nsum(rate(x_total{instance=\"$instance\"}[$__interval]))", want: "Q1"},
		{name: "comment_dquote_hides_job", expr: "# a \"quote\nsum(up{instance=\"$instance\", job=\"x\"})", want: "Q3"},
		{name: "comment_only_passes", expr: "sum(up{instance=\"$instance\"}) # instance=~\".+\" job=\"x\" rate(x[$__interval])", want: ""},
		{name: "single_quoted_escaped_name", expr: `sum(up{'inst\x61nce'=~".+"})`, want: "Q2"},
		{name: "single_quoted_escaped_job", expr: `sum(up{instance="$instance", 'j\x6fb'="x"})`, want: "Q3"},
		{name: "single_quoted_high_byte_value", expr: `sum(up{instance='$instance\x80'})`, want: "Q2"},
		{name: "escaped_dollar_value", expr: `sum(up{instance="\x24instance"})`, want: "Q2"},
		{name: "regex_instance", expr: `sum(up{instance=~"$instance|"})`, want: "Q2"},
		{name: "space_before_paren", expr: `sum(increase (x_total{instance="$instance"}[$__interval]))`, want: "Q1"},
		{name: "spaces_in_range", expr: `sum(increase(x_total{instance="$instance"}[ $__interval ]))`, want: "Q1"},
		{name: "subquery", expr: `increase(sum by (instance) (x_total{instance="$instance"})[$__interval:])`, want: "Q1"},
		{name: "subquery_step", expr: `rate(sum(x_total{instance="$instance"})[$__interval:1m])`, want: "Q1"},
		{name: "wrapped_subquery", expr: `rate(((sum(x_total{instance="$instance"}))[$__interval:1m]))`, want: "Q1"},
		{name: "wrapped_window", expr: `sum(rate((x_total{instance="$instance"}[$__interval])))`, want: "Q1"},
		{name: "nameless_window", expr: `sum(increase({__name__="x_total", instance="$instance"}[$__interval]))`, want: "Q1"},
		{name: "nameless_regex_instance", expr: `sum({__name__="x", instance=~".+"})`, want: "Q2"},
		{name: "bare_braces_instance", expr: `sum({instance=~"$instance"})`, want: "Q2"},
		{name: "single_quoted_value", expr: `sum(up{instance=~'$instance|'})`, want: "Q2"},
		{name: "backtick_value", expr: "sum(up{instance=~`$instance|`})", want: "Q2"},
		{name: "quoted_name", expr: `sum({"instance"=~"$instance|", __name__="up"})`, want: "Q2"},
		{name: "quoted_name_after_metric", expr: `sum(up{"instance"=~".+"})`, want: "Q2"},
		{name: "hex_escape_name", expr: `sum(up{"inst\x61nce"=~".+"})`, want: "Q2"},
		{name: "unicode_escape_name", expr: `sum(up{"inst\u0061nce"=~".+"})`, want: "Q2"},
		{name: "octal_escape_name", expr: `sum(up{"inst\141nce"=~".+"})`, want: "Q2"},
		{name: "single_quoted_unicode_name", expr: `sum(up{'inst\u0061nce'=~".+"})`, want: "Q2"},
		{name: "interval_ms", expr: `sum(rate(x_total{instance="$instance"}[${__interval_ms}ms]))`, want: "Q1"},
		{name: "field_path", expr: `sum(rate(x_total{instance="$instance"}[${__interval.x:raw}]))`, want: "Q1"},
		{name: "empty_alone", expr: `sum(up{instance=""})`, want: "Q2"},
		{name: "literal_stand_in", expr: `sum(up{instance="__var_instance__"})`, want: "Q2"},
		{name: "empty_beside_literal_stand_in", expr: `sum(up{instance="__var_instance__"}) or sum(up{instance=""})`, want: "Q2"},
		{name: "empty_beside_formatted", expr: `sum(up{instance="${instance:regex}"}) or sum(up{instance=""})`, want: "Q2"},
		{name: "empty_beside_sentinel", expr: `sum(up{instance="$instance"}) or sum(up{instance=""})`, want: ""},
		{name: "empty_beside_regex", expr: `sum(up{instance=~"$instance"}) or sum(up{instance=""})`, want: "Q2"},
		{name: "variable_query_result", kind: "variable", expr: `query_result(up{instance=~".+"})`, want: "Q2"},
		{name: "variable_query_result_window", kind: "variable", expr: `query_result(sum(rate(x_total{instance="$instance"}[$__interval])))`, want: "Q1"},
		{name: "label_values_filter", kind: "variable", expr: `label_values(up{instance=~".+"}, version)`, want: "Q2"},
		{name: "label_values_job", kind: "variable", variable: "instance", expr: `label_values(up{job="x"}, instance)`, want: "Q3"},
		{name: "series_selector", kind: "variable", expr: `up{instance=~".+"}`, want: "Q2"},
		{name: "label_values_bare_label", kind: "variable", expr: `label_values(instance)`, want: ""},
		{name: "label_values_empty_filter", kind: "variable", expr: `label_values({}, instance)`, want: ""},
		{name: "label_names", kind: "variable", expr: `label_names()`, want: ""},
		{name: "label_names_regex", kind: "variable", expr: `label_names(job|instance)`, want: ""},
		{name: "metrics_regex", kind: "variable", expr: `metrics(x_.*[a-z]+)`, want: ""},
		{name: "metrics_empty", kind: "variable", expr: `metrics()`, want: ""},
		// Grafana builds {__name__=~".*arg.*"} and {__name__=~"arg"} without escaping arg.
		{name: "label_names_breakout_job", kind: "variable", expr: `label_names(x",job="a)`, want: "Q3"},
		{name: "metrics_breakout_instance", kind: "variable", expr: `metrics(x",instance=~"a)`, want: "Q2"},
		{name: "metrics_breakout_hostname", kind: "variable", expr: `metrics(x",hostname="$instance)`, want: "Q3"},
		{name: "metrics_unescaped_backslash", kind: "variable", expr: `metrics(a\.b)`, want: "promql"},
		{name: "label_names_breakout_two_selectors", kind: "variable", expr: `label_names(x"} or {y="z)`, want: "promql"},
		{name: "interval_in_label_value", expr: `sum(rate(x_total{instance="$instance", path="[$__interval]"}[5m]))`, want: ""},
		{name: "interval_in_comment", expr: "sum(rate(x_total{instance=\"$instance\"}[5m])) # [$__interval]", want: ""},
		{name: "comment_paren", expr: "sum(rate(x_total{instance=\"$instance\"} # )\n[$__interval]))", want: "Q1"},
		{name: "comment_before_brace_job", expr: "sum(up # \"\n{instance=\"$instance\", job=\"x\"})", want: "Q3"},
		{name: "comment_before_brace_instance", expr: "sum(up # '\n{instance=~\".+\"})", want: "Q2"},
		{name: "comment_pair", expr: "# \"\nsum(up{instance=~\".+\"})\n# \"", want: "Q2"},
		{name: "name_twice", expr: `sum(up{__name__="up", instance="$instance"})`, want: "promql"},
		{name: "brace_in_value", expr: `sum(up{path="a}b", instance=~".+"})`, want: "Q2"},
		{name: "subquery_regex", expr: `max_over_time(up{instance=~".+"}[5m:])`, want: "Q2"},
		{name: "not_equal", expr: `sum(up{instance!="$instance"})`, want: "Q2"},
		{name: "over_time_one_interval", expr: `min_over_time(x{instance="$instance"}[$__interval])`, want: ""},
		{name: "last_over_time_bracket", expr: `last_over_time(x{instance="$instance"}[[[__interval]]])`, want: ""},
		{name: "offset_interval", expr: `sum(rate(x_total{instance="$instance"}[5m] offset $__interval))`, want: ""},
		{name: "rate_interval", expr: `sum(rate(x_total{instance="$instance"}[$__rate_interval]))`, want: ""},
		{name: "range", expr: `sum(increase(x_total{instance="$instance"}[$__range]))`, want: ""},
		{name: "fixed", expr: `sum(rate(x_total{instance="$instance"}[5m]))`, want: ""},
		{name: "backslash_value", expr: `sum(up{instance="\\$instance"})`, want: "Q2"},
		// Q2 presence: on a dashboard with an instance variable every selector carries an
		// instance matcher.
		{name: "presence_or_unscoped", expr: `sum(up{instance="$instance"}) or sum(up)`, want: "Q2"},
		{name: "presence_comment_pair", expr: "max by (scope) (renovate_scheduler_last_run_exit_code{instance=\"$instance\"}) # \"\nor renovate_scheduler_last_run_ok # \"\n", want: "Q2"},
		{name: "presence_window", expr: `sum(rate(x_total[5m]))`, want: "Q2"},
		{name: "presence_nameless", expr: `sum({__name__="up"})`, want: "Q2"},
		{name: "presence_subquery", expr: `max_over_time(sum(up)[5m:])`, want: "Q2"},
		{name: "presence_other_label_only", expr: `sum(up{version="1"})`, want: "Q2"},
		{name: "presence_no_selector_passes", expr: `vector(1)`, want: ""},
		{name: "presence_annotation", kind: "annotation", expr: `changes(up[5m]) > 0`, want: "Q2"},
		{name: "presence_variable_query_result", kind: "variable", variable: "attention", expr: `query_result(count(up))`, want: "Q2"},
		{name: "presence_variable_label_values", kind: "variable", variable: "version", expr: `label_values(x_build_info, version)`, want: "Q2"},
		{name: "presence_instance_variable_exempt", kind: "variable", variable: "instance", expr: `label_values(x_build_info, instance)`, want: ""},
		{name: "Q3_variable_label_name", expr: `sum(up{instance="$instance", $label="x"})`, want: "Q3"},
		// An ad-hoc filter is one matcher Grafana adds to every query.
		{name: "filter_job", kind: "filter", expr: `job="x"`, want: "Q3"},
		{name: "filter_instance_regex", kind: "filter", expr: `instance=~"a|b"`, want: "Q2"},
		{name: "filter_instance_exact_passes", kind: "filter", expr: `instance="$instance"`, want: ""},
		{name: "filter_other_label_passes", kind: "filter", expr: `worker="a"`, want: ""},
		{name: "filter_quote_breakout", kind: "filter", expr: `worker="a", job="x"`, want: "Q3"},
		{name: "filter_unparseable", kind: "filter", expr: `worker="a`, want: "promql"},
		// Q1 over duration expressions, which the stock parser accepts in a range.
		{name: "Q1_times_one", expr: `sum(rate(x_total{instance="$instance"}[$__interval * 1]))`, want: "Q1"},
		{name: "Q1_parenthesised_duration", expr: `sum(rate(x_total{instance="$instance"}[($__interval)]))`, want: "Q1"},
		{name: "Q1_min_of", expr: `sum(rate(x_total{instance="$instance"}[min_of($__interval, 1m)]))`, want: "Q1"},
		{name: "Q1_half", expr: `sum(rate(x_total{instance="$instance"}[$__interval / 2]))`, want: "Q1"},
		{name: "Q1_step", expr: `sum(rate(x_total{instance="$instance"}[step()]))`, want: "Q1"},
		{name: "Q1_interval_ms_seconds", expr: `sum(rate(x_total{instance="$instance"}[$__interval_ms / 1000]))`, want: "Q1"},
		{name: "Q1_interval_ms_times_ms", expr: `sum(rate(x_total{instance="$instance"}[$__interval_ms * 1ms]))`, want: "Q1"},
		{name: "Q1_times_ten_over_ten", expr: `sum(rate(x_total{instance="$instance"}[$__interval * 10 / 10]))`, want: "Q1"},
		{name: "Q1_subquery_duration", expr: `rate(sum(x_total{instance="$instance"})[$__interval_ms / 1000:1m])`, want: "Q1"},
		{name: "Q1_max_of_floor_passes", expr: `sum(rate(x_total{instance="$instance"}[max_of($__interval, 1m)]))`, want: ""},
		{name: "Q1_plus_one_ms", expr: `sum(rate(x_total{instance="$instance"}[$__interval + 1ms]))`, want: "Q1"},
		// A window holds two samples only from two sample spacings: the scrape interval for a
		// range, the step for a subquery.
		{name: "Q1_plus_five_seconds", expr: `sum(rate(x_total{instance="$instance"}[$__interval + 5s]))`, want: "Q1"},
		{name: "Q1_one_and_a_half", expr: `sum(rate(x_total{instance="$instance"}[$__interval * 1.5]))`, want: "Q1"},
		{name: "Q1_twice_less_one_ms", expr: `sum(rate(x_total{instance="$instance"}[$__interval * 2 - 1ms]))`, want: "Q1"},
		{name: "Q1_step_plus_one_ms", expr: `sum(rate(x_total{instance="$instance"}[step() + 1ms]))`, want: "Q1"},
		{name: "Q1_max_of_short_floor", expr: `sum(rate(x_total{instance="$instance"}[max_of($__interval, 20s)]))`, want: "Q1"},
		{name: "Q1_subquery_range_equals_step", expr: `rate(sum(x_total{instance="$instance"})[$__interval * 4:$__interval * 4])`, want: "Q1"},
		{name: "Q1_subquery_step_is_interval", expr: `rate(sum(x_total{instance="$instance"})[1h:$__interval])`, want: "Q1"},
		{name: "Q1_subquery_default_step", expr: `rate(sum(x_total{instance="$instance"})[$__interval * 4:])`, want: "Q1"},
		{name: "Q1_shrinks_as_interval_grows", expr: `sum(rate(x_total{instance="$instance"}[max_of(2m - $__interval, 10s)]))`, want: "Q1"},
		{name: "Q1_shrinks_past_the_long_reading", expr: `sum(rate(x_total{instance="$instance"}[max_of(3h - $__interval, 10s)]))`, want: "Q1"},
		{name: "Q1_rate_interval_less_interval", expr: `sum(rate(x_total{instance="$instance"}[$__rate_interval - $__interval]))`, want: "Q1"},
		// Only the Prometheus data source's backend replaces $__rate_interval and the $__range
		// family, and only as $name or ${name}; a variable query gets no $__rate_interval.
		{name: "backend_built_in_bracket_spelling", expr: `sum(rate(x_total{instance="$instance"}[[[__rate_interval]]]))`, want: "promql"},
		{name: "backend_built_in_format", expr: `sum(increase(x_total{instance="$instance"}[${__range:raw}]))`, want: "promql"},
		{name: "backend_built_in_braces_passes", expr: `sum(rate(x_total{instance="$instance"}[${__rate_interval}]))`, want: ""},
		{name: "variable_query_no_rate_interval", kind: "variable", expr: `query_result(sum(rate(x_total{instance="$instance"}[$__rate_interval])))`, want: "promql"},
		{name: "variable_query_range_seconds", kind: "variable", expr: `query_result(sum(x{instance="$instance"}) > $__range_s)`, want: ""},
		{name: "variable_query_range_any_spelling", kind: "variable", expr: `query_result(sum(increase(x_total{instance="$instance"}[[[__range]]])))`, want: ""},
		{name: "Q1_rate_interval_ms_passes", expr: `sum(rate(x_total{instance="$instance"}[${__rate_interval_ms}ms]))`, want: ""},
		{name: "Q1_subquery_max_of_passes", expr: `rate(sum(x_total{instance="$instance"})[max_of($__interval, 2m):1m])`, want: ""},
		{name: "Q1_subquery_two_steps_passes", expr: `rate(sum(x_total{instance="$instance"})[$__interval * 8:$__interval * 4])`, want: ""},
		{name: "Q1_step_times_four_passes", expr: `sum(rate(x_total{instance="$instance"}[step() * 4]))`, want: ""},
		{name: "window_division_by_zero_parse_error", expr: `sum(max_over_time(x{instance="$instance"}[1m / 0]))`, want: "promql"},
		{name: "offset_negative_passes", expr: `sum(rate(x_total{instance="$instance"}[5m] offset ($__interval - 1h)))`, want: ""},
		{name: "Q1_fixed_short_window_passes", expr: `sum(rate(x_total{instance="$instance"}[10s]))`, want: ""},
		{name: "Q1_sub_millisecond_more", expr: `sum(rate(x_total{instance="$instance"}[$__interval + 0.0004]))`, want: "Q1"},
		// Prometheus truncates both to whole milliseconds, 30000ms and 15000ms, before comparing.
		{name: "Q1_subquery_whole_milliseconds_passes", expr: `rate(sum(x_total{instance="$instance"})[$__interval * 2 + 0.0006:$__interval + 0.0004])`, want: ""},
		{name: "Q1_twice_passes", expr: `sum(rate(x_total{instance="$instance"}[$__interval * 2]))`, want: ""},
		{name: "Q1_plus_passes", expr: `sum(rate(x_total{instance="$instance"}[$__interval + 1m]))`, want: ""},
		{name: "Q1_fixed_duration_expression_passes", expr: `sum(rate(x_total{instance="$instance"}[30s * 2]))`, want: ""},
		{name: "Q1_one_side_of_a_binary", expr: `sum(rate(x_total{instance="$instance"}[5m])) / sum(rate(y_total{instance="$instance"}[$__interval]))`, want: "Q1"},
		{name: "Q1_interval_variable_passes", expr: `sum(rate(x_total{instance="$instance"}[$window]))`, want: ""},
		// An interval variable reads its shortest choice at the short reading.
		{name: "Q1_interval_variable_auto", expr: `sum(rate(x_total{instance="$instance"}[$auto_window]))`, want: "Q1"},
		{name: "Q1_interval_variable_short_option", expr: `sum(rate(x_total{instance="$instance"}[$fast_window]))`, want: "Q1"},
		{name: "Q1_interval_variable_subquery_step", expr: `rate(sum(x_total{instance="$instance"})[1m:$window])`, want: "Q1"},
		{name: "Q1_interval_variable_auto_floor_passes", expr: `sum(rate(x_total{instance="$instance"}[$auto_long_floor]))`, want: ""},
		{name: "Q1_interval_variable_over_time_passes", expr: `sum(max_over_time(x{instance="$instance"}[$fast_window]))`, want: ""},
		// Q3, each Q2 spelling mirrored.
		{name: "Q3_job", expr: `sum(up{instance="$instance", job="x"})`, want: "Q3"},
		{name: "Q3_quoted_job", expr: `sum(up{instance="$instance", "job"="x"})`, want: "Q3"},
		{name: "Q3_escaped_job", expr: `sum(up{instance="$instance", "j\x6fb"="x"})`, want: "Q3"},
		{name: "Q3_nameless_job", expr: `sum({__name__="up", instance="$instance", job="x"})`, want: "Q3"},
		{name: "Q3_negated_job", expr: `sum(up{instance="$instance", job!=""})`, want: "Q3"},
		{name: "Q3_hostname", expr: `sum(up{instance="$instance", hostname="h"})`, want: "Q3"},
		{name: "Q3_container", expr: `sum(up{instance="$instance", container=~".+"})`, want: "Q3"},
		{name: "Q3_by_job_passes", expr: `sum by (job) (up{instance="$instance"})`, want: ""},
		{name: "Q3_on_job_passes", expr: `up{instance="$instance"} * on (job) group_left () vector(1)`, want: ""},
		// Every rule a query breaks is reported, once per rule.
		{name: "all_three", expr: `sum(increase(up{instance=~"$instance|", job="x"}[${__interval_ms}ms]))`, want: "Q1,Q2,Q3"},
		{name: "annotation", kind: "annotation", expr: `idelta(x{instance="$instance"}[$__interval]) > 0`, want: "Q1"},
		{name: "restart_arm_passes", expr: `sum(resets(x_total{instance="$instance"}[$__interval]) > 0)`, want: ""},
		// Substitution leaves an unknown name as Grafana does.
		{name: "unknown_name_in_string_stays", expr: `label_replace(up{instance="$instance"}, "a", "$1", "b", "(.*)")`, want: ""},
		{name: "unknown_name_outside_string", expr: `sum(rate(x_total{instance="$instance"}[$nope]))`, want: "promql"},
		{name: "unparseable", expr: `sum(rate(x_total{instance="$instance"}[5m])`, want: "promql"},
		{name: "empty_panel_query", expr: ``, want: "promql"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			kind := c.kind
			if kind == "" {
				kind = "panel"
			}
			in := appQuery(kind, c.expr)
			in.Queries[0].Variable = c.variable
			assertRules(t, in, c.want)
		})
	}
}

// The eight ways a dashboard writes one $__interval each parse to the interval sentinel.
// step() and range() are 0 in an instant query, so Prometheus refuses a window built from them
// there; a panel query with both flags runs both ways.
func TestInstantQueries(t *testing.T) {
	for _, c := range []struct {
		name, kind, expr, want string
		instant, ranged        bool
	}{
		{name: "instant_range_window", expr: `sum(max_over_time(x{instance="$instance"}[range()]))`, instant: true, want: "promql"},
		{name: "instant_step_window", expr: `sum(rate(x_total{instance="$instance"}[step() * 4]))`, instant: true, want: "promql"},
		{name: "instant_and_range_step_window", expr: `sum(rate(x_total{instance="$instance"}[step() * 4]))`, instant: true, ranged: true, want: "promql"},
		{name: "query_result_step_window", kind: "variable", expr: `query_result(sum(rate(x_total{instance="$instance"}[step() * 4])))`, want: "promql"},
		{name: "instant_interval_window", expr: `sum(rate(x_total{instance="$instance"}[$__interval]))`, instant: true, want: "Q1"},
		{name: "instant_rate_interval_passes", expr: `sum(rate(x_total{instance="$instance"}[$__rate_interval]))`, instant: true, want: ""},
		{name: "range_range_window_passes", expr: `sum(rate(x_total{instance="$instance"}[range()]))`, ranged: true, want: ""},
		{name: "no_flag_range_window_passes", expr: `sum(rate(x_total{instance="$instance"}[range()]))`, want: ""},
		{name: "range_step_window_passes", expr: `sum(rate(x_total{instance="$instance"}[step() * 4]))`, ranged: true, want: ""},
		{name: "instant_and_range_step_floor", expr: `sum(rate(x_total{instance="$instance"}[max_of(step(), 10s)]))`, instant: true, ranged: true, want: "Q1"},
		{name: "instant_step_floor_passes", expr: `sum(rate(x_total{instance="$instance"}[max_of(step(), 10s)]))`, instant: true, want: ""},
	} {
		t.Run(c.name, func(t *testing.T) {
			kind := c.kind
			if kind == "" {
				kind = "panel"
			}
			in := appQuery(kind, c.expr)
			in.Queries[0].Instant, in.Queries[0].Range = c.instant, c.ranged
			assertRules(t, in, c.want)
		})
	}
	t.Run("message_names_the_instant_run", func(t *testing.T) {
		in := appQuery("panel", `sum(max_over_time(x{instance="$instance"}[range()]))`)
		in.Queries[0].Instant = true
		r := run(t, in)
		if len(r.Findings) != 1 {
			t.Fatalf("findings = %+v, want one", r.Findings)
		}
		f := r.Findings[0]
		if want := "as an instant query, where step() and range() are 0, at a $__interval of 1h0m37.123s, duration must be greater than 0"; !strings.Contains(f.Message, want) {
			t.Errorf("message %q does not hold %q", f.Message, want)
		}
		if want := "An instant query has no step or range"; !strings.Contains(f.Fix, want) {
			t.Errorf("fix %q does not hold %q", f.Fix, want)
		}
	})
}

func TestIntervalSpellings(t *testing.T) {
	for _, spelling := range []string{
		`$__interval`,
		`${__interval}`,
		`[[__interval]]`,
		`[[__interval:raw]]`,
		`${__interval:raw}`,
		`${__interval_ms}ms`,
		`${__interval_ms:raw}ms`,
		`[[__interval_ms]]ms`,
	} {
		t.Run(spelling, func(t *testing.T) {
			assertRules(t, appQuery("panel", `sum(rate(x_total{instance="$instance"}[`+spelling+`]))`), "Q1")
		})
	}
}

// Every function that drops a series with fewer than two samples in its window fails over one
// interval.
func TestTwoSampleFunctions(t *testing.T) {
	for _, call := range []string{
		`rate(x{instance="$instance"}[$__interval])`,
		`irate(x{instance="$instance"}[$__interval])`,
		`increase(x{instance="$instance"}[$__interval])`,
		`delta(x{instance="$instance"}[$__interval])`,
		`idelta(x{instance="$instance"}[$__interval])`,
		`deriv(x{instance="$instance"}[$__interval])`,
		`predict_linear(x{instance="$instance"}[$__interval], 3600)`,
	} {
		name := call[:strings.Index(call, "(")]
		t.Run(name, func(t *testing.T) {
			r := run(t, appQuery("panel", "sum("+call+")"))
			if len(r.Findings) != 1 || r.Findings[0].Rule != "Q1" {
				t.Fatalf("findings = %+v, want one Q1", r.Findings)
			}
			if !strings.Contains(r.Findings[0].Message, name+" ") {
				t.Errorf("message %q does not name %s", r.Findings[0].Message, name)
			}
		})
	}
}

// These answer from one sample, so one interval passes. resets and changes read 0 from it.
func TestOneSampleFunctions(t *testing.T) {
	for _, call := range []string{
		`avg_over_time(x{instance="$instance"}[$__interval])`,
		`min_over_time(x{instance="$instance"}[$__interval])`,
		`max_over_time(x{instance="$instance"}[$__interval])`,
		`sum_over_time(x{instance="$instance"}[$__interval])`,
		`count_over_time(x{instance="$instance"}[$__interval])`,
		`last_over_time(x{instance="$instance"}[$__interval])`,
		`stddev_over_time(x{instance="$instance"}[$__interval])`,
		`stdvar_over_time(x{instance="$instance"}[$__interval])`,
		`quantile_over_time(0.5, x{instance="$instance"}[$__interval])`,
		`present_over_time(x{instance="$instance"}[$__interval])`,
		`absent_over_time(x{instance="$instance"}[$__interval])`,
		`resets(x{instance="$instance"}[$__interval])`,
		`changes(x{instance="$instance"}[$__interval])`,
	} {
		t.Run(call[:strings.Index(call, "(")], func(t *testing.T) {
			assertRules(t, appQuery("panel", call), "")
		})
	}
}

// On a dashboard with an instance variable an ALERTS selector carries an instance matcher like any
// other. An alert about an absent target has no instance label, so instance="" alone reads it on
// ALERTS matched only by alertname and alertstate equalities.
func TestAlertsScope(t *testing.T) {
	for _, c := range []struct{ name, expr, want string }{
		{name: "alertname_only", expr: `0 * max(ALERTS{alertname="XTargetAbsent", alertstate="firing"})`, want: "Q2"},
		{name: "nameless_alertname_only", expr: `max({__name__="ALERTS", alertname="XTargetAbsent"})`, want: "Q2"},
		{name: "empty_alone_passes", expr: `max(ALERTS{alertname="XTargetAbsent", alertstate="firing", instance=""})`, want: ""},
		{name: "nameless_empty_alone_passes", expr: `max({instance="", __name__="ALERTS", alertname="XTargetAbsent"})`, want: ""},
		{name: "exact_passes", expr: `max(ALERTS{alertname="XDown", alertstate="firing", instance="$instance"})`, want: ""},
		{name: "pair_passes", expr: `max(ALERTS{alertname="X", instance="$instance"}) or max(ALERTS{alertname="X", instance=""})`, want: ""},
		{name: "pair_regex_name", expr: `ALERTS{alertname=~"X|Y", instance="$instance"} or ALERTS{alertname=~"X|Y", instance=""}`, want: "Q2"},
		{name: "wide_empty_beside_exact", expr: `max(up{instance="$instance"}) or count(ALERTS{alertname=~".+", instance=""})`, want: "Q2"},
		{name: "negated_state_empty_beside_exact", expr: `max(up{instance="$instance"}) or count(ALERTS{alertname="X", alertstate!="firing", instance=""})`, want: "Q2"},
		{name: "nameless_empty_beside_exact", expr: `max(up{instance="$instance"}) or max({alertname="X", instance=""})`, want: "Q2"},
		{name: "name_regex_empty_beside_exact", expr: `up{instance="$instance"} or {__name__=~"ALERTS|up", instance=""}`, want: "Q2"},
		{name: "other_name_regex_beside_exact", expr: `up{instance="$instance"} or {__name__=~"up|x", instance=""}`, want: ""},
		{name: "target_alert_and_exact", expr: `ALERTS{alertname="X", instance="$instance"} and ALERTS{alertname="X", instance=""}`, want: "Q2"},
		// The parser decodes names, quotes and comments, so none of these hides a selector.
		{name: "escaped_metric_name", expr: `max({__name__="\x41LERTS", alertname="XTargetAbsent"})`, want: "Q2"},
		{name: "space_before_brace", expr: `max(ALERTS {alertname="XTargetAbsent"})`, want: "Q2"},
		{name: "reordered_matchers", expr: `max(ALERTS{alertstate="firing", alertname="XTargetAbsent"})`, want: "Q2"},
		{name: "instance_in_comment", expr: "max(ALERTS{alertname=\"XTargetAbsent\" # , instance=\"$instance\"\n})", want: "Q2"},
		{name: "or_nameless", expr: `max(up{instance="$instance"}) or max({__name__="ALERTS", alertname="XTargetAbsent"})`, want: "Q2"},
		{name: "keyword_before_brace", expr: `max without (alertstate) ({__name__="ALERTS", alertname="XTargetAbsent"})`, want: "Q2"},
		// instance="" alone passes on that closed shape only; any other matcher is refused.
		{name: "empty_other_label", expr: `max(ALERTS{alertname="X", alertstate="firing", severity="page", instance=""})`, want: "Q2"},
		{name: "empty_no_alertname", expr: `max(ALERTS{alertstate="firing", instance=""})`, want: "Q2"},
		{name: "empty_regex_metric_name", expr: `max({__name__=~"ALERTS", alertname="X", instance=""})`, want: "Q2"},
		{name: "empty_other_metric", expr: `max(ALERTS_FOR_STATE{alertname="X", instance=""})`, want: "Q2"},
		{name: "empty_regex_alertname", expr: `count(ALERTS{alertname=~".+", instance=""})`, want: "Q2"},
		{name: "empty_negated_alertname", expr: `count(ALERTS{alertname!="", instance=""})`, want: "Q2"},
		{name: "empty_empty_alertname", expr: `count(ALERTS{alertname="", instance=""})`, want: "Q2"},
		{name: "empty_negated_alertstate", expr: `count(ALERTS{alertname="X", alertstate!="firing", instance=""})`, want: "Q2"},
		{name: "regex_beside_scoped", expr: `sum(ALERTS{alertname=~".+"}) or sum(up{instance="$instance"})`, want: "Q2"},
	} {
		t.Run(c.name, func(t *testing.T) {
			assertRules(t, appQuery("panel", c.expr), c.want)
		})
	}
}

// A series meets every matcher of its selector, so two instance matchers in one read nothing or
// hide one another.
func TestOneInstanceMatcherPerSelector(t *testing.T) {
	for _, c := range []struct{ name, expr string }{
		{name: "alerts_exact_and_empty", expr: `max(ALERTS{alertname="X", instance="$instance", instance=""})`},
		{name: "alerts_empty_first", expr: `max(ALERTS{instance="", alertname="X", instance="$instance"})`},
		{name: "alerts_escaped_name", expr: `max(ALERTS{alertname="X", "inst\x61nce"="$instance", instance=""})`},
		{name: "alerts_in_pair", expr: `max(ALERTS{alertname="X", instance="$instance", instance=""}) or max(ALERTS{alertname="X", instance=""})`},
		{name: "metric_exact_and_empty", expr: `sum(up{instance="$instance", instance=""})`},
		{name: "metric_exact_twice", expr: `sum(up{instance="$instance", instance="$instance"})`},
		{name: "regex_and_exact", expr: `sum(up{instance=~".+", instance="$instance"})`},
	} {
		t.Run(c.name, func(t *testing.T) {
			r := run(t, appQuery("panel", c.expr))
			if got := rulesOf(r); !slices.Equal(got, []string{"Q2"}) {
				t.Fatalf("rules = %v, want [Q2]; findings: %+v", got, r.Findings)
			}
			if !strings.Contains(r.Findings[0].Message, " instance matchers, and a series must meet every one") {
				t.Errorf("message %q does not name the instance matchers of one selector", r.Findings[0].Message)
			}
		})
	}
}

// instance="" selects series with no instance label, so beside instance="$instance" it is a second
// arm joined by or. The nearest join to a selector holding instance="$instance" decides.
func TestEmptyInstanceJoin(t *testing.T) {
	for _, c := range []struct{ name, expr, why string }{
		{name: "or_join", expr: `up{instance="$instance"} or up{instance=""}`},
		{name: "exact_on_right", expr: `up{instance=""} or up{instance="$instance"}`},
		{name: "or_through_parens_and_arithmetic", expr: `(sum(up{instance="$instance"})) or (0 * sum(up{instance=""}))`},
		{name: "or_ignoring", expr: `up{instance="$instance"} or ignoring(instance) up{instance=""}`},
		{name: "nearest_or_decides", expr: `(up{instance="$instance"} or up{instance=""}) and on() x{instance="$instance"}`},
		{name: "and_join", expr: `up{instance="$instance"} and up{instance=""}`, why: `instance="" is joined to the instance="$instance" selector by and;`},
		{name: "and_join_aggregated", expr: `count(up{instance="$instance"}) and count(up{instance=""})`, why: `instance="" is joined to the instance="$instance" selector by and;`},
		{name: "empty_on_left_and", expr: `up{instance=""} and up{instance="$instance"}`, why: `instance="" is joined to the instance="$instance" selector by and;`},
		{name: "unless_join", expr: `up{instance="$instance"} unless up{instance=""}`, why: `instance="" is joined to the instance="$instance" selector by unless;`},
		{name: "arithmetic_join", expr: `up{instance="$instance"} + up{instance=""}`, why: `instance="" is joined to the instance="$instance" selector by +;`},
		{name: "nearest_and_decides", expr: `x{instance="$instance"} or (y{instance="$instance"} and z{instance=""})`, why: `instance="" is joined to the instance="$instance" selector by and;`},
		{name: "or_of_empty_arms", expr: `up{instance="$instance"} and on() (a{instance=""} or b{instance=""})`, why: `instance="" is joined to the instance="$instance" selector by and;`},
		{name: "and_in_call_argument_under_or", expr: `w{instance="$instance"} or sum(clamp_min(x{instance="$instance"} and y{instance=""}, scalar(sum(v{instance="$instance"}))))`, why: `instance="" is joined to the instance="$instance" selector by and;`},
		{name: "exact_in_call_argument", expr: `histogram_quantile(scalar(x{instance="$instance"}), up{instance=""})`, why: `instance="" selects only series with no instance label`},
	} {
		t.Run(c.name, func(t *testing.T) {
			r := run(t, appQuery("panel", c.expr))
			if c.why == "" {
				if len(r.Findings) != 0 {
					t.Errorf("findings = %+v, want none", r.Findings)
				}
				return
			}
			if got := rulesOf(r); !slices.Equal(got, []string{"Q2"}) {
				t.Fatalf("rules = %v, want [Q2]; findings: %+v", got, r.Findings)
			}
			if !strings.Contains(r.Findings[0].Message, c.why) {
				t.Errorf("message %q does not say %q", r.Findings[0].Message, c.why)
			}
		})
	}
}

func TestFindingText(t *testing.T) {
	expr := `sum(increase(up{instance=~"$instance|", job="x"}[${__interval_ms}ms]))`
	in := appQuery("panel", expr)
	in.Queries[0].Where = `element panel-1 "Jobs" query 0`
	r := run(t, in)
	want := map[string]struct{ message, fix string }{
		"Q1": {"increase returns nothing from a single sample", "use [$__rate_interval], or [$__range] for a total over the picked range, and give a subquery a range of at least twice its step. An interval variable there needs every choice, auto_min included when auto is on, of at least twice the 15s scrape interval."},
		"Q2": {`instance=~"$instance|" matches it as a regular expression`, `match the instance exactly with one instance="$instance" in every selector. Put instance="" in a second selector joined by or beside it, or alone in ALERTS{alertname="<name>", instance=""}, optionally with alertstate="<state>"; a selector that can read ALERTS takes instance="" in that shape only.`},
		"Q3": {`job="x"`, `delete the matcher; the metric name and instance="$instance" select the app's series.`},
	}
	if len(r.Findings) != len(want) {
		t.Fatalf("findings = %+v, want Q1, Q2 and Q3", r.Findings)
	}
	for _, f := range r.Findings {
		t.Run(f.Rule, func(t *testing.T) {
			w := want[f.Rule]
			if f.Where != in.Queries[0].Where {
				t.Errorf("where = %q, want %q", f.Where, in.Queries[0].Where)
			}
			if !strings.Contains(f.Message, w.message) {
				t.Errorf("message %q does not hold %q", f.Message, w.message)
			}
			if quoted := `The query: "sum(increase(up{instance=~\"$instance|\", job=\"x\"}[${__interval_ms}ms]))".`; !strings.Contains(f.Message, quoted) {
				t.Errorf("message %q does not quote the query as written", f.Message)
			}
			if f.Fix != w.fix {
				t.Errorf("fix = %q, want %q", f.Fix, w.fix)
			}
		})
	}
}

func TestParseError(t *testing.T) {
	cases := []struct {
		name, kind, expr string
		holds            []string
	}{
		{
			name: "panel",
			kind: "panel",
			expr: `sum(rate(x_total{instance="$instance"}[$__rate_interval])`,
			holds: []string{
				"Prometheus's parser cannot read the query: ",
				"unclosed left parenthesis",
				`The query: "sum(rate(x_total{instance=\"$instance\"}[$__rate_interval])".`,
				`Grafana sends it as "sum(rate(x_total{instance=\"__var_instance__\"}[3652123ms])".`,
			},
		},
		{
			name:  "label_values selector",
			kind:  "variable",
			expr:  `label_values(up{instance=, version)`,
			holds: []string{"cannot read the label_values series selector: "},
		},
		{
			name:  "query_result expression",
			kind:  "variable",
			expr:  `query_result(sum(up)`,
			holds: []string{"cannot read the query_result expression: "},
		},
		{
			name:  "series selector",
			kind:  "variable",
			expr:  `up{`,
			holds: []string{"cannot read the series selector: "},
		},
		{
			name:  "metrics selector",
			kind:  "variable",
			expr:  `metrics(a\.b)`,
			holds: []string{`cannot read the selector {__name__=~"a\.b"} Grafana builds for metrics(): `},
		},
		{
			name:  "label_names selector",
			kind:  "variable",
			expr:  `label_names(a\d)`,
			holds: []string{`cannot read the selector {__name__=~".*a\d.*"} Grafana builds for label_names(): `},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			in := appQuery(c.kind, c.expr)
			in.Queries[0].Where = `element panel-7 "Queue depth" query 0`
			r := run(t, in)
			if len(r.Findings) != 1 {
				t.Fatalf("findings = %+v, want one", r.Findings)
			}
			f := r.Findings[0]
			if f.Rule != "promql" || f.Where != in.Queries[0].Where {
				t.Errorf("rule, where = %q, %q", f.Rule, f.Where)
			}
			for _, s := range c.holds {
				if !strings.Contains(f.Message, s) {
					t.Errorf("message %q does not hold %q", f.Message, s)
				}
			}
			if !strings.Contains(f.Fix, "$name must be a dashboard variable or a Grafana built-in") {
				t.Errorf("fix = %q", f.Fix)
			}
		})
	}
	t.Run("unchanged text is not repeated", func(t *testing.T) {
		r := run(t, appQuery("panel", `sum(up`))
		if len(r.Findings) != 1 || strings.Contains(r.Findings[0].Message, "Grafana sends it as") {
			t.Errorf("findings = %+v", r.Findings)
		}
	})
}

// A custom or text variable in a range sends text that is no duration, and the fix says so.
func TestStringVariableInRange(t *testing.T) {
	vars := append(slices.Clone(dashboardVars), variable{Name: "win", Kind: "CustomVariable"})
	for _, expr := range []string{
		`sum(rate(x_total{instance="$instance"}[$win]))`,
		`max_over_time(sum(x{instance="$instance"})[${win}:1m])`,
	} {
		t.Run(expr, func(t *testing.T) {
			r := run(t, input{App: true, Variables: vars, Queries: []query{{Where: "q", Kind: "panel", Expr: expr}}})
			want := "$win is not an interval variable, so its value is no duration; make it an IntervalVariable, or use [$__rate_interval]."
			if len(r.Findings) != 1 || r.Findings[0].Rule != "promql" || r.Findings[0].Fix != want {
				t.Errorf("findings = %+v, want one promql finding with fix %q", r.Findings, want)
			}
		})
	}
	t.Run("outside a range keeps the general fix", func(t *testing.T) {
		r := run(t, input{App: true, Variables: vars, Queries: []query{{Where: "q", Kind: "panel", Expr: `sum(x{instance="$instance"}) $win`}}})
		if len(r.Findings) != 1 || !strings.Contains(r.Findings[0].Fix, "$name must be a dashboard variable") {
			t.Errorf("findings = %+v", r.Findings)
		}
	})
}

// With no instance variable, $instance stays as written inside a string, so every instance
// matcher fails and the fix adds the variable.
func TestNoInstanceVariable(t *testing.T) {
	in := input{
		App:       true,
		Variables: []variable{{Name: "datasource", Kind: "DatasourceVariable"}},
		Queries:   []query{{Where: "q", Kind: "panel", Expr: `sum(up{instance="$instance"})`}},
	}
	r := run(t, in)
	if len(r.Findings) != 1 || r.Findings[0].Rule != "Q2" {
		t.Fatalf("findings = %+v, want one Q2", r.Findings)
	}
	if want := `add a single-select instance variable and match instance="$instance".`; r.Findings[0].Fix != want {
		t.Errorf("fix = %q, want %q", r.Findings[0].Fix, want)
	}
	if !strings.Contains(r.Findings[0].Message, "on a dashboard with no instance variable") {
		t.Errorf("message = %q", r.Findings[0].Message)
	}
}

// An unscoped selector is named, with what it reads; with no instance variable there is no
// instance to scope to, so an unscoped selector passes.
func TestUnscopedSelector(t *testing.T) {
	r := run(t, appQuery("panel", `sum(rate(x_total{version="$instance"}[5m])) or sum(up)`))
	if len(r.Findings) != 1 || r.Findings[0].Rule != "Q2" {
		t.Fatalf("findings = %+v, want one Q2", r.Findings)
	}
	for _, s := range []string{
		`x_total{version="$instance"} has no instance matcher, so it reads every instance's series`,
		`; up has no instance matcher`,
	} {
		if !strings.Contains(r.Findings[0].Message, s) {
			t.Errorf("message %q does not hold %q", r.Findings[0].Message, s)
		}
	}
	in := appQuery("panel", `sum(up)`)
	in.Variables = []variable{{Name: "datasource", Kind: "DatasourceVariable"}}
	assertRules(t, in, "")
}

// Q2 and Q3 hold only on an app dashboard; Q1 and parsing hold on every one.
func TestNotAppDashboard(t *testing.T) {
	for _, c := range []struct{ name, expr, want string }{
		{"Q1 still applies", `sum(rate(up{instance=~".+", hostname="h", job="x"}[$__interval]))`, "Q1"},
		{"Q2 and Q3 skipped", `sum(up{instance=~".+", hostname=~"$instance", container="c"})`, ""},
		{"parse error still applies", `sum(up`, "promql"},
	} {
		t.Run(c.name, func(t *testing.T) {
			in := appQuery("panel", c.expr)
			in.App = false
			assertRules(t, in, c.want)
		})
	}
}

func TestChecked(t *testing.T) {
	in := input{App: true, Variables: dashboardVars, Queries: []query{
		{Where: "a", Kind: "panel", Expr: `sum(up{instance="$instance"})`},
		{Where: "b", Kind: "variable", Expr: `label_names()`},
		{Where: "c", Kind: "variable", Expr: `label_values(up{instance="$instance"}, version)`},
		{Where: "d", Kind: "panel", Expr: `sum(up`},
		{Where: "e", Kind: "variable", Expr: `label_values({ }, instance)`},
	}}
	if r := run(t, in); r.Checked != 3 {
		t.Errorf("checked = %d, want 3 (label_names() and an empty label_values filter send no PromQL)", r.Checked)
	}
}

func TestUnknownKind(t *testing.T) {
	if _, err := check(appQuery("logql", `up`)); err == nil || !strings.Contains(err.Error(), `unknown query kind "logql"`) {
		t.Errorf("err = %v", err)
	}
}

func TestSubstitute(t *testing.T) {
	first, second := readings([]variable{
		{Name: "instance", Kind: "QueryVariable"},
		{Name: "instance_group", Kind: "CustomVariable"},
		{Name: "window", Kind: "IntervalVariable"},
		{Name: "fast", Kind: "IntervalVariable", Intervals: []string{"30s", "15s"}},
	})
	for _, c := range []struct{ in, first, second string }{
		{`$instance`, `__var_instance__`, `__alt_instance__`},
		{`${instance}`, `__var_instance__`, `__alt_instance__`},
		{`[[instance]]`, `__var_instance__`, `__alt_instance__`},
		{`${instance:raw}`, `__var_instance__`, `__alt_instance__`},
		{`[[instance:csv]]`, `__var_instance__`, `__alt_instance__`},
		{`${instance:Regex}`, `__var_instance__`, `__alt_instance__`},
		{`${instance:regex}`, `__var_instance__as_regex__`, `__alt_instance__as_regex__`},
		{`[[instance:percentencode]]`, `__var_instance__as_percentencode__`, `__alt_instance__as_percentencode__`},
		{`${instance:date:iso}`, `__var_instance__as_date__`, `__alt_instance__as_date__`},
		{`${instance.label:raw}`, `__var_instance__field__`, `__alt_instance__field__`},
		{`$instance_group`, `__var_instance_group__`, `__alt_instance_group__`},
		{`$window`, `3637126ms`, `3637126ms`},
		{`${fast}`, `3637127ms`, `15000ms`},
		{`$__interval`, `3637123ms`, `15000ms`},
		{`${__interval:regex}`, `3637123ms`, `15000ms`},
		{`${__interval_ms}ms`, `3637123ms`, `15000ms`},
		{`$__rate_interval $__range`, `3652123ms 7919s`, `60000ms 7919s`},
		{`$__range_s $__range_ms $__rate_interval_ms`, `7919 7919000 3652123`, `7919 7919000 60000`},
		{`$__from $__to`, `1700000000000 1700007919000`, `1700000000000 1700007919000`},
		{`$1 $nope [[nope]] ${nope}`, `$1 $nope [[nope]] ${nope}`, `$1 $nope [[nope]] ${nope}`},
		{`\x24instance`, `\x24instance`, `\x24instance`},
	} {
		t.Run(c.in, func(t *testing.T) {
			if got := substitute("panel", c.in, first); got != c.first {
				t.Errorf("first reading of %q = %q, want %q", c.in, got, c.first)
			}
			if got := substitute("panel", c.in, second); got != c.second {
				t.Errorf("second reading of %q = %q, want %q", c.in, got, c.second)
			}
		})
	}
}

// An interval variable's shortest value is its shortest choice: scenes 8.13.5 IntervalVariable
// and grafana v13.2.3 rangeutil.ts calculateInterval for the auto option.
func TestShortest(t *testing.T) {
	for _, c := range []struct {
		name string
		v    variable
		want time.Duration
		ok   bool
	}{
		{"shortest_option", variable{Intervals: []string{"10m", "30s", "1h"}}, 30 * time.Second, true},
		{"compound_option", variable{Intervals: []string{"1h", "1m30s"}}, 90 * time.Second, true},
		{"unreadable_option_skipped", variable{Intervals: []string{"1M", "5m"}}, 5 * time.Minute, true},
		{"no_readable_option", variable{Intervals: []string{"1M"}}, 0, false},
		{"auto_floor_below_options", variable{Intervals: []string{"1m"}, Auto: true, AutoMin: "10s", AutoCount: 30}, 10 * time.Second, true},
		{"option_below_auto_floor", variable{Intervals: []string{"5s"}, Auto: true, AutoMin: "10s", AutoCount: 30}, 5 * time.Second, true},
		{"auto_min_empty", variable{Intervals: []string{"1m"}, Auto: true, AutoCount: 30}, time.Millisecond, true},
		{"auto_count_zero", variable{Auto: true, AutoMin: "10s"}, 365 * 24 * time.Hour, true},
		{"auto_min_unreadable", variable{Intervals: []string{"1m"}, Auto: true, AutoMin: "1M", AutoCount: 30}, time.Minute, true},
		{"auto_off_ignores_floor", variable{Intervals: []string{"1m"}, AutoMin: "10s", AutoCount: 30}, time.Minute, true},
	} {
		t.Run(c.name, func(t *testing.T) {
			got, ok := shortest(&c.v)
			if got != c.want || ok != c.ok {
				t.Errorf("shortest = %v, %v; want %v, %v", got, ok, c.want, c.ok)
			}
		})
	}
}

// A Q1 failure on an interval variable names the variable's value at the reading that fails.
func TestIntervalVariableMessage(t *testing.T) {
	r := run(t, appQuery("panel", `sum(rate(x_total{instance="$instance"}[$auto_window])) / sum(rate(x_total{instance="$instance"}[${auto_window}]))`))
	if len(r.Findings) != 1 || r.Findings[0].Rule != "Q1" {
		t.Fatalf("findings = %+v, want one Q1", r.Findings)
	}
	want := "at a $__interval of 15s and $auto_window of 10s its window of 10s is under twice the 15s between its samples."
	if !strings.Contains(r.Findings[0].Message, want) {
		t.Errorf("message %q does not hold %q", r.Findings[0].Message, want)
	}
}

// A single-select variable whose options are all numbers sends a number, and a constant sends
// its duration; any other custom or constant variable sends text.
func TestNumericAndDurationVariables(t *testing.T) {
	vars := append(slices.Clone(dashboardVars),
		variable{Name: "n", Kind: "CustomVariable", Values: []string{"1", " 2.5"}},
		variable{Name: "multi_n", Kind: "CustomVariable", Multi: true, Values: []string{"1", "2"}},
		variable{Name: "all_n", Kind: "CustomVariable", IncludeAll: true, Values: []string{"1", "2"}},
		variable{Name: "words", Kind: "CustomVariable", Values: []string{"1", "a"}},
		variable{Name: "five", Kind: "ConstantVariable", Values: []string{"5m"}},
		variable{Name: "label_const", Kind: "ConstantVariable", Values: []string{"x"}},
	)
	for _, c := range []struct{ name, expr, want string }{
		{"numeric_comparison", `sum(x{instance="$instance"}) > $n`, ""},
		{"numeric_topk", `topk($n, sum by (a) (x{instance="$instance"}))`, ""},
		{"constant_window", `sum(rate(x_total{instance="$instance"}[$five]))`, ""},
		{"multi_numeric_topk", `topk($multi_n, sum by (a) (x{instance="$instance"}))`, "promql"},
		{"include_all_numeric_topk", `topk($all_n, sum by (a) (x{instance="$instance"}))`, "promql"},
		{"mixed_options_topk", `topk($words, sum by (a) (x{instance="$instance"}))`, "promql"},
		{"mixed_options_window", `sum(rate(x_total{instance="$instance"}[$words]))`, "promql"},
		{"text_constant_topk", `topk($label_const, sum by (a) (x{instance="$instance"}))`, "promql"},
		{"numeric_in_window_is_seconds", `sum(rate(x_total{instance="$instance"}[$n]))`, ""},
	} {
		t.Run(c.name, func(t *testing.T) {
			assertRules(t, input{App: true, Variables: vars, Queries: []query{{Where: "q", Kind: "panel", Expr: c.expr}}}, c.want)
		})
	}
}

// An interval variable offering an option Prometheus cannot read is reported where it is used.
func TestUnreadableIntervalOption(t *testing.T) {
	vars := append(slices.Clone(dashboardVars),
		variable{Name: "typo", Kind: "IntervalVariable", Intervals: []string{"1m", "1M", "2Y"}},
		variable{Name: "big", Kind: "IntervalVariable", Intervals: []string{"2h", "1d"}},
	)
	run1 := func(expr string) report {
		return run(t, input{App: true, Variables: vars, Queries: []query{{Where: "q", Kind: "panel", Expr: expr}}})
	}
	r := run1(`sum(rate(x_total{instance="$instance"}[$typo])) + sum(rate(y_total{instance="$instance"}[${typo}]))`)
	if len(r.Findings) != 1 || r.Findings[0].Rule != "promql" {
		t.Fatalf("findings = %+v, want one promql", r.Findings)
	}
	if want := `$typo offers "1M", "2Y", which Prometheus reads as no duration.`; !strings.Contains(r.Findings[0].Message, want) {
		t.Errorf("message %q does not hold %q", r.Findings[0].Message, want)
	}
	if want := "give the interval variable only Prometheus durations (ms, s, m, h, d, w, y)."; r.Findings[0].Fix != want {
		t.Errorf("fix = %q, want %q", r.Findings[0].Fix, want)
	}
	if r := run1(`sum(rate(x_total{instance="$instance"}[$window]))`); len(r.Findings) != 0 {
		t.Errorf("an unreferenced variable's options were reported: %+v", r.Findings)
	}
	// Its shortest choice is over an hour, so the window grows from the short reading to the long one.
	if r := run1(`sum(rate(x_total{instance="$instance"}[$big]))`); len(r.Findings) != 0 {
		t.Errorf("findings = %+v, want none", r.Findings)
	}
}

// A window that shrinks as $__interval grows is named as such.
func TestShrinkingWindowMessage(t *testing.T) {
	r := run(t, appQuery("panel", `sum(rate(x_total{instance="$instance"}[max_of(3h - $__interval, 10s)]))`))
	if len(r.Findings) != 1 {
		t.Fatalf("findings = %+v, want one", r.Findings)
	}
	if want := "its window shrinks as $__interval grows, so at long ranges it holds a single sample"; !strings.Contains(r.Findings[0].Message, want) {
		t.Errorf("message %q does not hold %q", r.Findings[0].Message, want)
	}
}

// A format that changes the value names itself in the message; one that keeps it passes.
func TestInstanceFormats(t *testing.T) {
	for _, c := range []struct{ expr, holds string }{
		{`sum(up{instance="${instance:regex}"})`, `instance="${instance:regex}" sends the value regex-escaped`},
		{`sum(up{instance="${instance:percentencode}"})`, `instance="${instance:percentencode}" sends the value percent-encoded`},
		{`sum(up{instance="[[instance:text]]"})`, `instance="${instance:text}" sends the value as the option's display text`},
		{`sum(up{instance="${instance:lucene}"})`, `sends the value Lucene-escaped`},
		{`sum(up{instance="${instance.label}"})`, `instance="${instance.field}" sends a field of the picked option`},
		{`sum(up{instance="__var_instance__"})`, `instance="__var_instance__" writes out the check's stand-in`},
		{`sum(up{instance="$instance:9100"})`, `instance="$instance:9100" adds text to $instance`},
	} {
		t.Run(c.expr, func(t *testing.T) {
			r := run(t, appQuery("panel", c.expr))
			if len(r.Findings) != 1 || r.Findings[0].Rule != "Q2" {
				t.Fatalf("findings = %+v, want one Q2", r.Findings)
			}
			if !strings.Contains(r.Findings[0].Message, c.holds) {
				t.Errorf("message %q does not hold %q", r.Findings[0].Message, c.holds)
			}
		})
	}
	for _, expr := range []string{
		`sum(up{instance="${instance:raw}"})`,
		`sum(up{instance="[[instance:csv]]"})`,
		`sum(up{instance="${instance:glob}"})`,
		`sum(up{instance="${instance:Regex}"})`,
	} {
		t.Run(expr, func(t *testing.T) {
			assertRules(t, appQuery("panel", expr), "")
		})
	}
}

// Only a single-select instance variable sends one value an exact matcher can equal.
func TestMultiValueInstanceVariable(t *testing.T) {
	for _, c := range []struct {
		name string
		v    variable
	}{
		{"multi", variable{Name: "instance", Kind: "QueryVariable", Multi: true}},
		{"include all", variable{Name: "instance", Kind: "QueryVariable", IncludeAll: true}},
	} {
		t.Run(c.name, func(t *testing.T) {
			in := input{App: true, Variables: []variable{c.v}, Queries: []query{{Where: "q", Kind: "panel", Expr: `sum(up{instance="$instance"})`}}}
			r := run(t, in)
			if len(r.Findings) != 1 || r.Findings[0].Rule != "Q2" {
				t.Fatalf("findings = %+v, want one Q2", r.Findings)
			}
			if !strings.Contains(r.Findings[0].Message, "multi-select or includes All") {
				t.Errorf("message = %q", r.Findings[0].Message)
			}
			if want := `make the instance variable single-select, with multi and includeAll false, and match instance="$instance".`; r.Findings[0].Fix != want {
				t.Errorf("fix = %q, want %q", r.Findings[0].Fix, want)
			}
		})
	}
}

// display shows each dashboard variable's sentinel as the $name it stands for, longest first.
func TestMatcherDisplay(t *testing.T) {
	in := input{App: true, Variables: []variable{
		{Name: "instance", Kind: "QueryVariable"},
		{Name: "instance_b", Kind: "QueryVariable"},
	}, Queries: []query{{Where: "q", Kind: "panel", Expr: `sum(up{instance=~"$instance_b|$instance"})`}}}
	r := run(t, in)
	if len(r.Findings) != 1 || !strings.Contains(r.Findings[0].Message, `instance=~"$instance_b|$instance" matches`) {
		t.Errorf("findings = %+v", r.Findings)
	}
}

// A duration Prometheus refuses at either interval is one promql finding naming that interval
// and Prometheus's reason (promql/durations.go calculateDuration).
func TestRefusedDuration(t *testing.T) {
	for _, c := range []struct{ name, expr, holds string }{
		{"window_zero", `sum(rate(x_total{instance="$instance"}[$__interval - $__interval]))`, "at a $__interval of 1h0m37.123s, duration must be greater than 0"},
		{"window_negative_when_zoomed", `sum(rate(x_total{instance="$instance"}[$__interval - 1m]))`, "at a $__interval of 15s, duration must be greater than 0"},
		{"window_negative_at_the_long_interval", `sum(rate(x_total{instance="$instance"}[1h - $__interval]))`, "at a $__interval of 1h0m37.123s, duration must be greater than 0"},
		{"window_division_by_zero", `sum(max_over_time(x{instance="$instance"}[1m / (step() - step())]))`, "at a $__interval of 1h0m37.123s, division by zero"},
		{"window_modulo_by_zero", `sum(max_over_time(x{instance="$instance"}[1m % (step() - step())]))`, "at a $__interval of 1h0m37.123s, modulo by zero"},
		{"window_nan", `sum(rate(x_total{instance="$instance"}[(0 - $__interval) ^ 0.5]))`, "at a $__interval of 1h0m37.123s, duration is NaN or infinite"},
		{"window_out_of_range", `sum(rate(x_total{instance="$instance"}[$__interval * 1e7]))`, "at a $__interval of 1h0m37.123s, duration is out of range"},
		{"subquery_step", `max_over_time(sum(x{instance="$instance"})[1h:$__interval - 1m])`, "at a $__interval of 15s, duration must be greater than 0"},
		{"subquery_offset", `max_over_time(sum(x{instance="$instance"})[1h:] offset ((0 - step()) ^ 0.5))`, "at a $__interval of 1h0m37.123s, duration is NaN or infinite"},
		{"offset", `sum(x{instance="$instance"} offset ((0 - step()) ^ 0.5))`, "at a $__interval of 1h0m37.123s, duration is NaN or infinite"},
		{"offset_out_of_range", `sum(x{instance="$instance"} offset (0 - step() * 1e7))`, "at a $__interval of 1h0m37.123s, duration is out of range"},
	} {
		t.Run(c.name, func(t *testing.T) {
			r := run(t, appQuery("panel", c.expr))
			if len(r.Findings) != 1 || r.Findings[0].Rule != "promql" {
				t.Fatalf("findings = %+v, want one promql", r.Findings)
			}
			if want := "Prometheus refuses a duration of the query: " + c.holds + "."; !strings.Contains(r.Findings[0].Message, want) {
				t.Errorf("message %q does not hold %q", r.Findings[0].Message, want)
			}
		})
	}
}
