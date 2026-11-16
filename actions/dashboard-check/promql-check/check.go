package main

import (
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/prometheus/common/model"
	"github.com/prometheus/prometheus/promql/parser"
)

// kindVariable is the query kind of a QueryVariable's query; rulePromql is the rule a query
// Prometheus refuses breaks.
const (
	kindVariable = "variable"
	rulePromql   = "promql"
)

type input struct {
	Variables []variable `json:"variables"`
	Queries   []query    `json:"queries"`
	App       bool       `json:"app"`
}

// variable is one dashboard variable. Intervals, Auto, AutoMin and AutoCount are an
// IntervalVariable's choices as Grafana reads them; Values are a CustomVariable's options or a
// ConstantVariable's value.
type variable struct {
	Name       string   `json:"name"`
	Kind       string   `json:"kind"`
	AutoMin    string   `json:"autoMin"`
	Intervals  []string `json:"intervals"`
	Values     []string `json:"values"`
	AutoCount  int      `json:"autoCount"`
	Multi      bool     `json:"multi"`
	IncludeAll bool     `json:"includeAll"`
	Auto       bool     `json:"auto"`
}

// query is one Prometheus query as the dashboard writes it. Kind is panel or annotation for a
// PromQL expression, variable for a QueryVariable's query, which has its own grammar, and filter
// for one ad-hoc filter's matcher; Variable names that QueryVariable. Instant and Range are the
// query's own flags, and Grafana runs a query with neither as a range query.
type query struct {
	Where    string `json:"where"`
	Kind     string `json:"kind"`
	Expr     string `json:"expr"`
	Variable string `json:"variable,omitempty"`
	Instant  bool   `json:"instant,omitempty"`
	Range    bool   `json:"range,omitempty"`
}

type finding struct {
	Where   string `json:"where"`
	Rule    string `json:"rule"`
	Message string `json:"message"`
	Fix     string `json:"fix"`
}

type report struct {
	Findings []finding `json:"findings"`
	Checked  int       `json:"checked"`
}

// The first reading's $__interval and the dashboard's time range, which is the same in both readings.
const (
	intervalMs = 3637123
	rangeS     = 7919
	fromMs     = 1700000000000
)

const interval = intervalMs * time.Millisecond

// shortInterval is the Prometheus data source's default scrape interval (@grafana/prometheus
// 13.1.2 datasource.ts, `jsonData.timeInterval || "15s"`): the spacing of a range's samples, and
// the $__interval Grafana sends once a user zooms in to its floor.
const shortInterval = 15 * time.Second

// rateInterval is Grafana's $__rate_interval at one $__interval: grafana-prometheus-datasource
// 7ca48f8f (@grafana/prometheus 13.1.2) pkg/promlib/models/query.go calculateRateInterval.
func rateInterval(iv time.Duration) time.Duration {
	return max(iv+shortInterval, 4*shortInterval)
}

// changedFormats are the formats that send a single string value other than as written: scenes
// 8.13.5 (grafana v13.2.3's pin) formatRegistry.ts. raw, pipe, distributed, csv, json, glob and
// join return it unchanged, and an unknown format falls back to glob.
var changedFormats = map[string]string{
	"lucene":           "Lucene-escaped",
	"regex":            "regex-escaped",
	"html":             "HTML-escaped",
	"percentencode":    "percent-encoded",
	"uriencode":        "URI-encoded",
	"singlequote":      "in single quotes",
	"doublequote":      "in double quotes",
	"sqlstring":        "as a quoted SQL string",
	"date":             "as a date",
	"text":             "as the option's display text",
	"queryparam":       "as a URL parameter",
	"customqueryparam": "as a URL parameter",
}

// reading is one set of values for every variable. check reads each query at two: a long one,
// where $__interval and every interval variable are unique durations of about an hour, and the
// shortest Grafana sends, a 15s $__interval and each interval variable's shortest choice.
// $__rate_interval follows $__interval and the time range is the same in both. Each string
// variable has a different stand-in in each, so a stand-in written by hand, the same in both,
// is told apart from a reference.
type reading struct {
	values map[string]string
	// backend are the built-ins only the Prometheus data source's backend replaces; see value.
	backend map[string]string
	// strings are the dashboard variables whose stand-in is a string, which a format changes.
	strings map[string]bool
	// intervals are the interval variables' values, which a message names.
	intervals map[string]time.Duration
	iv        time.Duration
}

func newReading(vars []variable, prefix string, number int, iv time.Duration, pick func(i int, v *variable) time.Duration) reading {
	r := reading{values: make(map[string]string, len(vars)+9), backend: map[string]string{}, strings: map[string]bool{}, intervals: map[string]time.Duration{}, iv: iv}
	for i, v := range vars {
		if v.Kind == "IntervalVariable" {
			d := pick(i, &vars[i])
			r.values[v.Name] = strconv.FormatInt(d.Milliseconds(), 10) + "ms"
			r.intervals[v.Name] = d
		} else if text, ok := stand(&vars[i], number+i); ok {
			r.values[v.Name] = text
		} else {
			r.values[v.Name] = prefix + v.Name + "__"
			r.strings[v.Name] = true
		}
	}
	// Grafana's scoped built-ins win over a dashboard variable of the same name. Each is written
	// in the same token shape in both readings, so both texts parse to the same tree.
	ms := strconv.FormatInt(iv.Milliseconds(), 10)
	rate := strconv.FormatInt(rateInterval(iv).Milliseconds(), 10)
	maps.Copy(r.values, map[string]string{
		"__interval":    ms + "ms",
		"__interval_ms": ms,
		"__from":        strconv.Itoa(fromMs),
		"__to":          strconv.Itoa(fromMs + rangeS*1000),
	})
	maps.Copy(r.backend, map[string]string{
		"__rate_interval":    rate + "ms",
		"__rate_interval_ms": rate,
		"__range":            strconv.Itoa(rangeS) + "s",
		"__range_ms":         strconv.Itoa(rangeS * 1000),
		"__range_s":          strconv.Itoa(rangeS),
	})
	for name := range r.values {
		if strings.HasPrefix(name, "__") {
			delete(r.strings, name)
			delete(r.intervals, name)
		}
	}
	for name := range r.backend {
		delete(r.values, name)
		delete(r.strings, name)
		delete(r.intervals, name)
	}
	return r
}

// stand is the stand-in of a variable whose value is no string: a ConstantVariable holding a
// duration sends it as written, and a single-select variable whose every value is a number sends
// number, unique to the variable and the reading and larger at the long one, so a window of it
// does not read as shrinking. ok is false for every other variable.
func stand(v *variable, number int) (string, bool) {
	if len(v.Values) == 0 {
		return "", false
	}
	if v.Kind == "ConstantVariable" {
		if _, err := model.ParseDuration(v.Values[0]); err == nil {
			return v.Values[0], true
		}
	}
	if v.Multi || v.IncludeAll {
		return "", false
	}
	for _, s := range v.Values {
		if _, err := strconv.ParseFloat(strings.TrimSpace(s), 64); err != nil {
			return "", false
		}
	}
	return strconv.Itoa(number), true
}

// readings are the two readings check parses every query at.
func readings(vars []variable) (first, second reading) {
	// An interval variable's long value is never under its shortest, so a window over it grows
	// from the short reading to the long one, as one over $__interval does.
	long := func(i int, v *variable) time.Duration {
		d := interval + time.Duration(1+i)*time.Millisecond
		if s, ok := shortest(v); ok {
			return max(d, s+time.Duration(i)*time.Millisecond)
		}
		return d
	}
	short := func(i int, v *variable) time.Duration {
		if d, ok := shortest(v); ok {
			return d
		}
		return long(i, v)
	}
	return newReading(vars, "__var_", 7919501, interval, long), newReading(vars, "__alt_", 7919001, shortInterval, short)
}

// shortest is the shortest value an interval variable sends, among its intervals and its auto
// option's floor; an interval Prometheus reads as no duration is skipped. ok is false when no
// value is left, and the variable then reads the same in both readings.
func shortest(v *variable) (time.Duration, bool) {
	var all []time.Duration
	for _, s := range v.Intervals {
		if d, err := model.ParseDuration(s); err == nil {
			all = append(all, time.Duration(d))
		}
	}
	if d, ok := autoFloor(v); ok {
		all = append(all, d)
	}
	if len(all) == 0 {
		return 0, false
	}
	return slices.Min(all), true
}

// autoFloor is the shortest value of v's auto option: scenes 8.13.5 IntervalVariable sends
// grafana v13.2.3 rangeutil.ts calculateInterval, the time range over auto_count steps raised to
// auto_min, or to 1ms when auto_min is empty. An auto_count of 0 divides the range by zero, which
// roundInterval sends as 1y.
func autoFloor(v *variable) (time.Duration, bool) {
	switch {
	case !v.Auto:
		return 0, false
	case v.AutoCount == 0:
		return 365 * 24 * time.Hour, true
	case v.AutoMin == "":
		return time.Millisecond, true
	}
	d, err := model.ParseDuration(v.AutoMin)
	return time.Duration(d), err == nil
}

// at names the reading for a message about expr: its $__interval and the value of each interval
// variable expr references.
func (r reading) at(expr string) string {
	var b strings.Builder
	fmt.Fprintf(&b, "a $__interval of %v", r.iv)
	var named []string
	for _, m := range variableRegex.FindAllStringSubmatch(expr, -1) {
		name := m[1] + m[2] + m[4]
		if d, ok := r.intervals[name]; ok && !slices.Contains(named, name) {
			named = append(named, name)
			fmt.Fprintf(&b, " and $%s of %v", name, d)
		}
	}
	return b.String()
}

// value is what one reference sends: a field path or a changing format marks a string
// variable's stand-in, so the rules see that the value is not the variable's. Only the Prometheus
// data source's backend replaces $__rate_interval and the $__range family in a panel or annotation
// query, and reads only $name and ${name}; a variable query gets the $__range family in every
// spelling and no $__rate_interval (@grafana/prometheus 13.1.2 metric_find_query.ts and
// grafana-prometheus-datasource pkg/promlib/models/query.go). plain says the reference is one of
// the two forms.
func (r reading) value(kind, name, field, format string, plain bool) (string, bool) {
	if v, ok := r.backend[name]; ok {
		if kind == kindVariable {
			return v, !strings.HasPrefix(name, "__rate_interval")
		}
		return v, plain
	}
	v, ok := r.values[name]
	if !ok || !r.strings[name] {
		return v, ok
	}
	format, _, _ = strings.Cut(format, ":")
	switch {
	case field != "":
		return v + "field__", true
	case changedFormats[format] != "":
		return v + "as_" + format + "__", true
	}
	return v, true
}

// variableRegex is grafana v13.2.3 public/app/features/variables/utils.ts variableRegex
// ($x, [[x]], [[x:fmt]], ${x}, ${x.path}, ${x:fmt}), unchanged in meaning: JavaScript's \w is
// ASCII, as RE2's is. Groups: 1, 2 or 4 the name, 3 or 6 the format, 5 the field path.
var variableRegex = regexp.MustCompile(`\$(\w+)|\[\[(\w+?)(?::(\w+))?\]\]|\$\{(\w+)(?:\.([^:^}]+))?(?::([^}]+))?\}`)

// substitute replaces each variable reference the way Grafana's sceneInterpolator does, on the
// text as written and before PromQL decodes anything. A name with no value is left as written,
// as Grafana leaves an unknown variable, so `$1` in a label_replace replacement stays `$1`.
func substitute(kind, expr string, r reading) string {
	group := func(m []int, g int) string {
		if m[2*g] < 0 {
			return ""
		}
		return expr[m[2*g]:m[2*g+1]]
	}
	var b strings.Builder
	last := 0
	for _, m := range variableRegex.FindAllStringSubmatchIndex(expr, -1) {
		name := group(m, 1) + group(m, 2) + group(m, 4)
		field, format := group(m, 5), group(m, 3)+group(m, 6)
		value, ok := r.value(kind, name, field, format, group(m, 2) == "" && field == "" && format == "")
		if !ok {
			continue
		}
		b.WriteString(expr[last:m[0]])
		b.WriteString(value)
		last = m[1]
	}
	b.WriteString(expr[last:])
	return b.String()
}

// The Prometheus datasource's variable query forms: @grafana/prometheus 13.1.2
// migrations/variableMigration.ts, tried in metric_find_query.ts's order. jsDot and jsSpace are
// JavaScript's `.` and `\s`, which RE2's differ from.
const (
	jsDot   = `[^\n\r\x{2028}\x{2029}]`
	jsSpace = `[\t\n\v\f\r \x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}\x{feff}]`
)

var (
	labelNamesMatchQuery = jsRegexp(`^label_names\((.+)\)\s*$`)
	labelNamesQuery      = jsRegexp(`^label_names\(\)\s*$`)
	labelValuesQuery     = jsRegexp(`^label_values\((?:(.+),\s*)?(.+)\)\s*$`)
	metricNamesQuery     = jsRegexp(`^metrics\((.+)\)\s*$`)
	queryResultQuery     = jsRegexp(`^query_result\((.+)\)\s*$`)
	emptyForms           = []string{"label_values()", "metrics()", "query_result()"}
)

func jsRegexp(pattern string) *regexp.Regexp {
	pattern = strings.ReplaceAll(pattern, `.`, jsDot)
	return regexp.MustCompile(strings.ReplaceAll(pattern, `\s`, jsSpace))
}

// promql is the stock server's parser: experimental functions, fill modifiers and unquoted UTF-8
// names off, so a query a default Prometheus refuses fails here too.
var promql = parser.NewParser(parser.Options{})

// parsed is one PromQL text a query sends, with what it is called in a message. partial marks a
// selector the author writes only part of, a metric-name regex or an ad-hoc filter Grafana adds
// to other queries, so it needs no instance matcher; a quote that breaks out into another
// matcher is still held to Q2 and Q3. instant and ranged say whether Grafana sends it as an
// instant query, a range query or both.
type parsed struct {
	expr                     parser.Expr
	what                     string
	partial, instant, ranged bool
}

// runs lists each way Grafana sends p, true for an instant query: @grafana/prometheus 13.1.2
// datasource.ts processTargetV2 splits a query with both flags into two.
func (p parsed) runs() []bool {
	var out []bool
	if p.ranged {
		out = append(out, false)
	}
	if p.instant {
		out = append(out, true)
	}
	return out
}

// parseQuery returns what q sends to Prometheus after substitution, parsed. A variable query
// that sends no PromQL (label_names(), a bare label) returns ok false.
func parseQuery(q query, text string) (p parsed, ok bool, err error) {
	switch q.Kind {
	case "panel", "annotation":
		expr, err := promql.ParseExpr(text)
		// promlib models/query.go: a query with neither flag runs as a range query.
		return parsed{expr: expr, what: "the query", instant: q.Instant, ranged: q.Range || !q.Instant}, true, err
	case kindVariable:
		return parseVariable(text)
	case "filter":
		p, ok, err := selector("the ad-hoc filter's matcher", "{"+text+"}")
		p.partial = true
		return p, ok, err
	}
	return parsed{}, false, fmt.Errorf("%s: unknown query kind %q", q.Where, q.Kind)
}

func parseVariable(text string) (p parsed, ok bool, err error) {
	if m := labelNamesMatchQuery.FindStringSubmatch(text); m != nil {
		return nameSelector("label_names", `{__name__=~".*`+m[1]+`.*"}`)
	}
	if text == "" || labelNamesQuery.MatchString(text) {
		return parsed{}, false, nil
	}
	if m := labelValuesQuery.FindStringSubmatch(text); m != nil {
		// isFilterDefined: a blank filter or {} sends no match[].
		if m[1] == "" || strings.ReplaceAll(m[1], " ", "") == "{}" {
			return parsed{}, false, nil
		}
		return selector("the label_values series selector", m[1])
	}
	if m := metricNamesQuery.FindStringSubmatch(text); m != nil {
		return nameSelector("metrics", `{__name__=~"`+m[1]+`"}`)
	}
	if m := queryResultQuery.FindStringSubmatch(text); m != nil {
		// metric_find_query.ts queryResultQuery sends it to /api/v1/query.
		expr, err := promql.ParseExpr(m[1])
		return parsed{expr: expr, what: "the query_result expression", instant: true}, true, err
	}
	if slices.Contains(emptyForms, text) {
		return parsed{}, false, nil
	}
	return selector("the series selector", text)
}

// selector parses text as Prometheus parses a match[] parameter, wrapped as a VectorSelector so
// every rule walks it like any other expression.
func selector(what, text string) (parsed, bool, error) {
	matchers, err := promql.ParseMetricSelector(text)
	return parsed{expr: &parser.VectorSelector{LabelMatchers: matchers}, what: what, ranged: true}, true, err
}

// nameSelector parses the match[] selector metric_find_query.ts builds for label_names(regex)
// and metrics(regex): the regex between quotes, unescaped.
func nameSelector(form, sel string) (parsed, bool, error) {
	p, ok, err := selector(fmt.Sprintf("the selector %s Grafana builds for %s()", sel, form), sel)
	p.partial = true
	return p, ok, err
}

// check parses every query and applies the rules to each one that parses. A query's text is
// substituted and parsed at both readings, so a rule can tell what a value depends on. Only an
// unknown query kind is an error: every problem with a query is a finding.
func check(in input) (report, error) {
	first, second := readings(in.Variables)
	r := rules{first: first, second: second, app: in.App}
	if i := slices.IndexFunc(in.Variables, func(v variable) bool { return v.Name == instanceLabel }); i >= 0 {
		r.instanceVar = &in.Variables[i]
	}
	out := report{Findings: []finding{}}
	for _, q := range in.Queries {
		out.Findings = append(out.Findings, unreadableIntervals(q, in.Variables)...)
		text := substitute(q.Kind, q.Expr, first)
		p, ok, err := parseQuery(q, text)
		switch {
		case err != nil && !ok:
			return report{}, err
		case !ok:
			continue
		}
		out.Checked++
		if err != nil {
			out.Findings = append(out.Findings, parseFailure(q, p.what, text, first, err))
			continue
		}
		secondText := substitute(q.Kind, q.Expr, second)
		sp, _, err := parseQuery(q, secondText)
		if err != nil {
			out.Findings = append(out.Findings, parseFailure(q, p.what+" at "+second.at(q.Expr), secondText, second, err))
			continue
		}
		out.Findings = append(out.Findings, r.apply(q, p, sp.expr)...)
	}
	return out, nil
}

// unreadableIntervals reports each interval variable q reads that offers an option Prometheus
// reads as no duration, which shortest skips and so no window over it would be tested.
func unreadableIntervals(q query, vars []variable) []finding {
	var out []finding
	var seen []string
	for _, m := range variableRegex.FindAllStringSubmatch(q.Expr, -1) {
		name := m[1] + m[2] + m[4]
		i := slices.IndexFunc(vars, func(v variable) bool { return v.Name == name && v.Kind == "IntervalVariable" })
		if i < 0 || slices.Contains(seen, name) {
			continue
		}
		seen = append(seen, name)
		var bad []string
		for _, s := range vars[i].Intervals {
			if _, err := model.ParseDuration(s); err != nil {
				bad = append(bad, strconv.Quote(s))
			}
		}
		if len(bad) > 0 {
			out = append(out, finding{
				Where:   q.Where,
				Rule:    rulePromql,
				Message: fmt.Sprintf("$%s offers %s, which Prometheus reads as no duration. The query: %q.", name, strings.Join(bad, ", "), q.Expr),
				Fix:     "give the interval variable only Prometheus durations (ms, s, m, h, d, w, y).",
			})
		}
	}
	return out
}

func parseFailure(q query, what, text string, r reading, err error) finding {
	msg := fmt.Sprintf("Prometheus's parser cannot read %s: %v. The query: %q.", what, err, q.Expr)
	if text != q.Expr {
		msg += fmt.Sprintf(" Grafana sends it as %q.", text)
	}
	fix := "write a query Prometheus parses once Grafana substitutes its variables; " +
		"outside a string every $name must be a dashboard variable or a Grafana built-in, " +
		"because Grafana sends an unknown one as written."
	if name := r.inRange(text); name != "" {
		fix = fmt.Sprintf("$%s is not an interval variable, so its value is no duration; make it an IntervalVariable, or use [$__rate_interval].", name)
	}
	return finding{Where: q.Where, Rule: rulePromql, Message: msg, Fix: fix}
}

// inRange names the string variable whose stand-in text holds inside a [...] range, or "".
func (r reading) inRange(text string) string {
	for _, name := range slices.Sorted(maps.Keys(r.strings)) {
		if regexp.MustCompile(`\[[^\]]*` + regexp.QuoteMeta(r.values[name])).MatchString(text) {
			return name
		}
	}
	return ""
}
