package main

import (
	"errors"
	"fmt"
	"maps"
	"math"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/prometheus/common/model"
	"github.com/prometheus/prometheus/model/labels"
	"github.com/prometheus/prometheus/promql/parser"
)

const (
	extrapolated = "returns nothing from a single sample (extrapolatedRate)"
	instant      = "returns nothing from fewer than two samples (instantValue)"
	regression   = "returns nothing from fewer than two samples (its linear regression)"
)

// twoSampleFunctions are the range functions that drop a series whose window holds fewer than
// two samples, each with why: Prometheus v3.16.0 promql/functions.go. resets and changes are not
// here: from one sample they return 0, so a panel over them never goes blank.
var twoSampleFunctions = map[string]string{
	"rate":           extrapolated,
	"increase":       extrapolated,
	"delta":          extrapolated,
	"irate":          instant,
	"idelta":         instant,
	"deriv":          regression,
	"predict_linear": regression,
}

// instanceLabel is the label Q2 holds to the dashboard variable of the same name.
const instanceLabel = "instance"

// userChosenLabels are set by the user's scrape config or log collector, never by the app.
var userChosenLabels = []string{"job", "hostname", "container"}

// rules applies Q1 to every query and Q2 and Q3 to an app dashboard's.
type rules struct {
	instanceVar   *variable
	first, second reading
	app           bool
}

// problems holds what one query breaks, one phrase per offending call, window or matcher.
type problems struct {
	refused, q1, q2, q3 []string
	instant             bool
}

// apply checks one query, parsed from its text at the first reading (first) and at the second
// (second).
func (r *rules) apply(q query, first parsed, second parser.Expr) []finding {
	var p problems
	expr := first.expr
	for _, instant := range first.runs() {
		p.windows(r, q.Expr, first.expr, second, instant)
	}
	if r.app {
		ms := matchers(expr, second)
		exact := slices.ContainsFunc(ms, r.exact)
		for _, m := range ms {
			switch {
			case m.first.Name == instanceLabel:
				if why := r.instanceMatcher(m, exact); why != "" {
					p.q2 = append(p.q2, why)
				}
			case m.second != nil && m.second.Name != m.first.Name:
				// The name holds a variable's stand-in, which differs between the readings.
				p.q3 = append(p.q3, r.display(m.first.String())+" takes its label name from a variable, so the check cannot hold it to Q2 or Q3")
			case slices.Contains(userChosenLabels, m.first.Name):
				p.q3 = append(p.q3, r.display(m.first.String()))
			}
		}
		if !first.partial {
			p.q2 = append(p.q2, r.unscoped(q, expr)...)
		}
	}
	return r.findings(q, &p)
}

// windows adds the refused durations and Q1 calls of one way Grafana sends the query, true for
// an instant query.
func (p *problems) windows(r *rules, text string, first, second parser.Expr, instant bool) {
	long, short := clock{step: r.first.iv, span: rangeS * time.Second}, clock{step: r.second.iv, span: rangeS * time.Second}
	if instant {
		long, short = clock{}, clock{}
	}
	refused, calls := r.q1(text, windows(first, long), windows(second, short))
	for _, why := range refused {
		if instant {
			why, p.instant = "as an instant query, where step() and range() are 0, "+why, true
		}
		p.refused = appendNew(p.refused, why)
	}
	for _, call := range calls {
		p.q1 = appendNew(p.q1, call)
	}
}

// unscoped lists the selectors of q that read every instance: on a dashboard with an instance
// variable, each selector needs an instance matcher, except in that variable's own query.
func (r *rules) unscoped(q query, expr parser.Expr) []string {
	if r.instanceVar == nil || (q.Kind == kindVariable && q.Variable == instanceLabel) {
		return nil
	}
	var out []string
	for _, vs := range selectors(expr) {
		if !slices.ContainsFunc(vs.LabelMatchers, func(m *labels.Matcher) bool { return m.Name == instanceLabel }) && !targetAlert(vs) {
			out = appendNew(out, r.display(vs.String())+" has no instance matcher, so it reads every instance's series")
		}
	}
	return out
}

// targetAlert is ALERTS matched only by alertname="<name>" and at most an alertstate="<state>": an
// alert about a target that is absent carries no instance label, so instance="$instance" would
// hide it. Any other operator reads every instance's alerts.
func targetAlert(vs *parser.VectorSelector) bool {
	alerts, named := false, false
	for _, m := range vs.LabelMatchers {
		switch m.Name {
		case model.MetricNameLabel:
			alerts = m.Type == labels.MatchEqual && m.Value == "ALERTS"
		case "alertname":
			if m.Type != labels.MatchEqual || m.Value == "" {
				return false
			}
			named = true
		case "alertstate":
			if m.Type != labels.MatchEqual {
				return false
			}
		default:
			return false
		}
	}
	return alerts && named
}

// matcher is one label matcher as each reading parsed it; second is nil when the two trees do
// not align.
type matcher struct {
	first, second *labels.Matcher
}

// matchers lists every vector selector's matchers in tree order. The readings differ only in
// tokens of one shape, so both trees hold the same matchers in the same places.
func matchers(expr, second parser.Expr) []matcher {
	a, b := selectors(expr), selectors(second)
	var out []matcher
	for i, vs := range a {
		for j, m := range vs.LabelMatchers {
			pair := matcher{first: m}
			if i < len(b) && j < len(b[i].LabelMatchers) {
				pair.second = b[i].LabelMatchers[j]
			}
			out = append(out, pair)
		}
	}
	return out
}

func selectors(expr parser.Expr) []*parser.VectorSelector {
	var out []*parser.VectorSelector
	parser.Inspect(expr, func(node parser.Node, _ []parser.Node) error {
		if vs, ok := node.(*parser.VectorSelector); ok {
			out = append(out, vs)
		}
		return nil
	})
	return out
}

func (r *rules) singleSelect() bool {
	return r.instanceVar != nil && !r.instanceVar.Multi && !r.instanceVar.IncludeAll
}

// exact is instance="$instance": the value is the instance variable's stand-in in both readings.
func (r *rules) exact(m matcher) bool {
	return r.singleSelect() && m.first.Name == instanceLabel && m.first.Type == labels.MatchEqual &&
		m.first.Value == r.first.values[instanceLabel] &&
		m.second != nil && m.second.Value == r.second.values[instanceLabel]
}

func (r *rules) findings(q query, p *problems) []finding {
	var out []finding
	quote := fmt.Sprintf(" The query: %q.", q.Expr)
	if len(p.refused) > 0 {
		fix := "write durations Prometheus accepts at every $__interval: a window or subquery step stays positive, such as [$__rate_interval], and none divides by zero or is infinite."
		if p.instant {
			fix += " An instant query has no step or range, so use $__interval or $__range there instead of step() or range()."
		}
		out = append(out, finding{
			Where:   q.Where,
			Rule:    rulePromql,
			Message: "Prometheus refuses a duration of the query: " + strings.Join(p.refused, "; ") + "." + quote,
			Fix:     fix,
		})
	}
	if len(p.q1) > 0 {
		out = append(out, finding{
			Where:   q.Where,
			Rule:    "Q1",
			Message: "a window that follows $__interval or an interval variable spans less than two sample spacings at some value of it, so it holds a single sample there and the panel goes empty: " + strings.Join(p.q1, "; ") + "." + quote,
			Fix:     "use [$__rate_interval], or [$__range] for a total over the picked range, and give a subquery a range of at least twice its step. An interval variable there needs every choice, auto_min included when auto is on, of at least twice the 15s scrape interval.",
		})
	}
	if len(p.q2) > 0 {
		fix := `match the instance exactly in every selector with instance="$instance"; instance="" may stay only beside it.`
		switch {
		case r.instanceVar == nil:
			fix = `add a single-select instance variable and match instance="$instance".`
		case !r.singleSelect():
			fix = `make the instance variable single-select, with multi and includeAll false, and match instance="$instance".`
		}
		out = append(out, finding{Where: q.Where, Rule: "Q2", Message: "the instance is not matched exactly: " + strings.Join(p.q2, "; ") + "." + quote, Fix: fix})
	}
	if len(p.q3) > 0 {
		out = append(out, finding{
			Where:   q.Where,
			Rule:    "Q3",
			Message: "a matcher on a label the user's scrape config or collector names, so the panel is empty for anyone who names it otherwise: " + strings.Join(p.q3, ", ") + "." + quote,
			Fix:     `delete the matcher; the metric name and instance="$instance" select the app's series.`,
		})
	}
	return out
}

// instanceMatcher says why one instance matcher fails Q2, or "" when it passes. exact says the
// query also holds instance="$instance".
func (r *rules) instanceMatcher(pair matcher, exact bool) string {
	m := pair.first
	shown := r.display(m.String())
	switch {
	case r.instanceVar == nil:
		return shown + " on a dashboard with no instance variable"
	case !r.singleSelect():
		return shown + " on a dashboard whose instance variable is multi-select or includes All, so Grafana sends a regular expression of the picked values"
	case m.Type == labels.MatchRegexp || m.Type == labels.MatchNotRegexp:
		return shown + " matches it as a regular expression, and Grafana does not escape a single-select variable, so an IPv6 address or a dot in a host name selects the wrong target or none"
	case m.Type != labels.MatchEqual:
		return shown + " selects every other instance"
	case r.exact(pair):
		return ""
	case m.Value == "" && exact:
		return ""
	case m.Value == "":
		return shown + ` selects only series with no instance label; it may stay only beside instance="$instance"`
	}
	return r.instanceValue(pair, shown)
}

// instanceValue says why an instance="..." matcher with a value other than $instance fails.
func (r *rules) instanceValue(pair matcher, shown string) string {
	m, want := pair.first, r.first.values[instanceLabel]
	format, _ := strings.CutPrefix(m.Value, want+"as_")
	format, formatted := strings.CutSuffix(format, "__")
	switch {
	case m.Value == want:
		return m.String() + " writes out the check's stand-in for $instance instead of referencing the variable"
	case m.Value == want+"field__":
		return shown + " sends a field of the picked option, not its value"
	case formatted && changedFormats[format] != "":
		return shown + " sends the value " + changedFormats[format] + ", which no longer equals the target's instance label"
	case pair.second == nil || m.Value != pair.second.Value:
		return shown + " adds text to $instance, so it never equals the target's instance label"
	case strings.Contains(m.Value, "$"):
		return shown + " keeps a $ Grafana never replaces, because it substitutes before PromQL decodes an escape such as \\x24 and leaves an unknown $name as written"
	}
	return shown + " pins one instance instead of the dashboard's instance variable"
}

// display prints a matcher or selector with each dashboard variable's stand-in shown as the
// reference it stands for, longest name first, so __var_a__ never rewrites part of __var_a__b__.
func (r *rules) display(s string) string {
	names := slices.SortedFunc(maps.Keys(r.first.strings), func(a, b string) int { return len(b) - len(a) })
	for _, name := range names {
		stand := regexp.QuoteMeta(r.first.values[name])
		s = regexp.MustCompile(stand+`as_([a-z]+)__`).ReplaceAllString(s, "$${"+name+":$1}")
		s = strings.ReplaceAll(s, r.first.values[name]+"field__", "${"+name+".field}")
		s = strings.ReplaceAll(s, r.first.values[name], "$"+name)
	}
	return s
}

// window is one duration a query sends, as Prometheus evaluates it at one $__interval: a range,
// or a subquery step or offset, which only Prometheus's refusal applies to.
type window struct {
	err error
	// fn is the two-sample function a range is an argument of, or "".
	fn  string
	rng time.Duration
	// spacing is the time between the samples a range reads: the scrape interval for a
	// MatrixSelector, the step for a SubqueryExpr.
	spacing time.Duration
}

// defaultEvaluationInterval is the step Prometheus gives a subquery that omits one: the global
// evaluation_interval, 1m by default (config.DefaultGlobalConfig, engine.go
// getLastSubqueryInterval).
const defaultEvaluationInterval = time.Minute

// clock is what step() and range() evaluate to in one query: Prometheus engine.go hands
// durationVisitor the query's step and its end minus start, both 0 for an instant query. Grafana
// sets a range query's step to its interval and its range to the dashboard's.
type clock struct {
	step, span time.Duration
}

// windows lists every duration of expr Prometheus evaluates, in tree order (promql/durations.go
// durationVisitor): each MatrixSelector and SubqueryExpr range, subquery step and offset.
func windows(expr parser.Expr, c clock) []window {
	args := twoSampleArgs(expr)
	var out []window
	rangeOf := func(node parser.Node, rng time.Duration, e *parser.DurationExpr, spacing time.Duration) {
		w := window{fn: args[node], rng: rng, spacing: spacing}
		if e != nil {
			w.rng, w.err = evalWindow(e, c, false)
		}
		out = append(out, w)
	}
	refusal := func(e *parser.DurationExpr, offset bool) {
		if e != nil {
			_, err := evalWindow(e, c, offset)
			out = append(out, window{err: err})
		}
	}
	parser.Inspect(expr, func(node parser.Node, _ []parser.Node) error {
		switch n := node.(type) {
		case *parser.MatrixSelector:
			rangeOf(node, n.Range, n.RangeExpr, shortInterval)
		case *parser.SubqueryExpr:
			rangeOf(node, n.Range, n.RangeExpr, subqueryStep(n, c))
			refusal(n.StepExpr, false)
			refusal(n.OriginalOffsetExpr, true)
		case *parser.VectorSelector:
			refusal(n.OriginalOffsetExpr, true)
		}
		return nil
	})
	return out
}

// twoSampleArgs maps each argument of a two-sample function, parentheses stripped, to its name.
func twoSampleArgs(expr parser.Expr) map[parser.Node]string {
	args := map[parser.Node]string{}
	parser.Inspect(expr, func(node parser.Node, _ []parser.Node) error {
		if n, ok := node.(*parser.Call); ok {
			if _, two := twoSampleFunctions[n.Func.Name]; two {
				for _, a := range n.Args {
					args[stripParens(a)] = n.Func.Name
				}
			}
		}
		return nil
	})
	return args
}

// subqueryStep is n's step at one clock. A step Prometheus refuses is its own window in windows,
// so here it is 0.
func subqueryStep(n *parser.SubqueryExpr, c clock) time.Duration {
	switch {
	case n.StepExpr != nil:
		d, _ := evalWindow(n.StepExpr, c, false)
		return d
	case n.Step == 0:
		return defaultEvaluationInterval
	}
	return n.Step
}

func stripParens(e parser.Expr) parser.Expr {
	for {
		p, ok := e.(*parser.ParenExpr)
		if !ok {
			return e
		}
		e = p.Expr
	}
}

// q1 pairs the windows of the query expr read at the first reading (long) and at the second
// (short); both texts parse to the same tree, so the lists align by position. It returns the
// windows Prometheus refuses and the Q1 calls: a two-sample function's window that reads an
// interval, its range or step differing between the readings, and at either reading spans less
// than two sample spacings, or shrinks from the short reading to the long one, which a finite set
// of readings cannot bound. Prometheus 3's ranges are left-open, so only a window of two spacings
// holds two samples at every evaluation time.
func (r *rules) q1(expr string, long, short []window) (refused, calls []string) {
	longAt, shortAt := r.first.at(expr), r.second.at(expr)
	for i := range min(len(long), len(short)) {
		l, s := long[i], short[i]
		switch {
		case l.err != nil:
			refused = appendNew(refused, fmt.Sprintf("at %s, %v", longAt, l.err))
		case s.err != nil:
			refused = appendNew(refused, fmt.Sprintf("at %s, %v", shortAt, s.err))
		case l.fn == "" || (l.rng == s.rng && l.spacing == s.spacing):
		case s.rng < 2*s.spacing:
			calls = appendNew(calls, sparse(l.fn, s, shortAt))
		case l.rng < 2*l.spacing:
			calls = appendNew(calls, sparse(l.fn, l, longAt))
		case l.rng < s.rng:
			calls = appendNew(calls, fmt.Sprintf("%s %s, and its window shrinks as $__interval grows, so at long ranges it holds a single sample", l.fn, twoSampleFunctions[l.fn]))
		}
	}
	return refused, calls
}

// sparse describes fn's window w, too short at the reading named at.
func sparse(fn string, w window, at string) string {
	return fmt.Sprintf("%s %s, and at %s its window of %v is under twice the %v between its samples", fn, twoSampleFunctions[fn], at, w.rng, w.spacing)
}

func appendNew(list []string, s string) []string {
	if slices.Contains(list, s) {
		return list
	}
	return append(list, s)
}

// evalWindow is Prometheus promql/durations.go calculateDuration: the expression's seconds,
// refused unless finite, in range and, but for an offset, positive, then truncated to whole
// milliseconds.
func evalWindow(expr parser.Expr, c clock, offset bool) (time.Duration, error) {
	seconds, err := evalDuration(expr, c)
	switch {
	case err != nil:
		return 0, err
	case math.IsNaN(seconds) || math.IsInf(seconds, 0):
		return 0, errors.New("duration is NaN or infinite")
	case seconds <= 0 && !offset:
		return 0, errors.New("duration must be greater than 0")
	case seconds > 1<<63/1e9 || seconds < -(1<<63)/1e9:
		return 0, errors.New("duration is out of range")
	}
	return time.Duration(seconds*1000) * time.Millisecond, nil
}

// evalDuration is Prometheus promql/durations.go evaluateDurationExpr.
func evalDuration(expr parser.Expr, c clock) (float64, error) {
	switch n := expr.(type) {
	case *parser.NumberLiteral:
		return n.Val, nil
	case *parser.DurationExpr:
		switch n.Op {
		case parser.STEP:
			return c.step.Seconds(), nil
		case parser.RANGE:
			return c.span.Seconds(), nil
		}
		var lhs, rhs float64
		var err error
		if n.LHS != nil {
			if lhs, err = evalDuration(n.LHS, c); err != nil {
				return 0, err
			}
		}
		if n.RHS != nil {
			if rhs, err = evalDuration(n.RHS, c); err != nil {
				return 0, err
			}
		}
		return durationOp(n.Op, lhs, rhs)
	}
	return 0, fmt.Errorf("unexpected duration expression type %T", expr)
}

// durationOp is one duration operator; a unary sign arrives with lhs 0.
func durationOp(op parser.ItemType, lhs, rhs float64) (float64, error) {
	switch op {
	case parser.MIN_OF:
		return math.Min(lhs, rhs), nil
	case parser.MAX_OF:
		return math.Max(lhs, rhs), nil
	case parser.ADD:
		return lhs + rhs, nil
	case parser.SUB:
		return lhs - rhs, nil
	case parser.MUL:
		return lhs * rhs, nil
	case parser.DIV:
		if rhs == 0 {
			return 0, errors.New("division by zero")
		}
		return lhs / rhs, nil
	case parser.MOD:
		if rhs == 0 {
			return 0, errors.New("modulo by zero")
		}
		return math.Mod(lhs, rhs), nil
	case parser.POW:
		return math.Pow(lhs, rhs), nil
	}
	return 0, fmt.Errorf("unexpected duration expression operator %q", op)
}
