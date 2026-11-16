// Command go-os-files reads Go file paths, one per line on stdin, relative to the
// working directory, and prints "<goos>\t<path>" for each file go/build builds
// for darwin or windows and not for linux at the same GOARCH. It exits 1 when
// go/build cannot read a file.
package main

import (
	"bufio"
	"fmt"
	"go/build"
	"os"
	"path/filepath"
	"strings"
)

// go-os-ci.yaml's cross-vet covers these GOARCHes on every leg.
var goarches = []string{"amd64", "arm64"}

var legs = []string{"darwin", "windows"}

// archLevels are the GOAMD64 and GOARM64 defaults' tags, which go/build reads
// from ToolTags (go help buildconstraint).
var archLevels = map[string][]string{"amd64": {"amd64.v1"}, "arm64": {"arm64.v8.0"}}

func buildContext(goos, goarch string) *build.Context {
	c := build.Default
	c.GOOS, c.GOARCH = goos, goarch
	// go-ci.yaml and go-os-ci.yaml run the race tests with CGO_ENABLED=1.
	c.CgoEnabled = true
	c.ToolTags = append([]string(nil), archLevels[goarch]...)
	for _, t := range build.Default.ToolTags {
		if strings.HasPrefix(t, "goexperiment.") {
			c.ToolTags = append(c.ToolTags, t)
		}
	}
	return &c
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "go-os-files:", err)
		os.Exit(1)
	}
}

func run() error {
	in := bufio.NewScanner(os.Stdin)
	out := bufio.NewWriter(os.Stdout)
	for in.Scan() {
		path := in.Text()
		if path == "" {
			continue
		}
		oses, err := builtOnlyOff(filepath.Dir(path), filepath.Base(path))
		if err != nil {
			return err
		}
		for _, goos := range oses {
			fmt.Fprintf(out, "%s\t%s\n", goos, path)
		}
	}
	if err := in.Err(); err != nil {
		return err
	}
	return out.Flush()
}

// builtOnlyOff returns each leg OS that builds dir/name at some GOARCH where
// linux does not.
func builtOnlyOff(dir, name string) ([]string, error) {
	var oses []string
	for _, goos := range legs {
		for _, goarch := range goarches {
			onLeg, err := buildContext(goos, goarch).MatchFile(dir, name)
			if err != nil {
				return nil, err
			}
			onLinux, err := buildContext("linux", goarch).MatchFile(dir, name)
			if err != nil {
				return nil, err
			}
			if onLeg && !onLinux {
				oses = append(oses, goos)
				break
			}
		}
	}
	return oses, nil
}
