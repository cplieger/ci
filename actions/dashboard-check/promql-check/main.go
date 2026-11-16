// Command promql-check reads one dashboard's Prometheus queries as JSON on stdin and writes, as
// JSON on stdout, every query Prometheus's parser refuses once Grafana has substituted its
// variables, and every query-shape rule a parsed query breaks.
//
// Input is the JSON form of input and output the JSON form of report (check.go). Exit 0 whatever
// it finds, 2 on unreadable input.
package main

import (
	"encoding/json"
	"fmt"
	"os"
)

func main() {
	dec := json.NewDecoder(os.Stdin)
	dec.DisallowUnknownFields()
	var in input
	if err := dec.Decode(&in); err != nil {
		fmt.Fprintf(os.Stderr, "promql-check: reading the queries: %v\n", err)
		os.Exit(2)
	}
	out, err := check(in)
	if err != nil {
		fmt.Fprintf(os.Stderr, "promql-check: %v\n", err)
		os.Exit(2)
	}
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		fmt.Fprintf(os.Stderr, "promql-check: writing the report: %v\n", err)
		os.Exit(2)
	}
}
