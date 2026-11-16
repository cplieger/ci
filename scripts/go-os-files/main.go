// Command go-os-files reads "<module dir>\t<path>" lines on stdin, both relative
// to the working directory, and prints "<goos>\t<module dir>\t<path>" for each
// leg OS, darwin or windows, that builds the file at a GOARCH, amd64 or arm64,
// where linux, darwin or windows excludes it. go/build decides each with the
// module's Go release tags and the target's own tool tags. It exits 1 when
// go/build cannot read a file or the module's go.mod.
package main

import (
	"bufio"
	"context"
	"fmt"
	"go/build"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// go-os-ci.yaml's cross-vet covers these GOARCHes on every leg.
var goarches = []string{"amd64", "arm64"}

var (
	runnerOSes = []string{"linux", "darwin", "windows"}
	legs       = []string{"darwin", "windows"}
)

const toolTagsFlag = "-print-tool-tags"

// setup-go's parseGoVersionFile picks the toolchain line, else the go line,
// unless GOTOOLCHAIN=local, which go-os-ci.yaml does not set:
// https://github.com/actions/setup-go/blob/b7ad1dad31e06c5925ef5d2fc7ad053ef454303e/src/installer.ts#L645
var (
	toolchainLine = regexp.MustCompile(`(?m)^toolchain go(1\.\d+(?:\.\d+|rc\d+)?)`)
	goLine        = regexp.MustCompile(`(?m)^go (\d+(\.\d+)*)`)
)

type target struct{ goos, goarch string }

type matcher struct {
	toolTags    map[target][]string
	releaseTags map[string][]string
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == toolTagsFlag {
		fmt.Println(strings.Join(build.Default.ToolTags, " "))
		return
	}
	if err := run(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, "go-os-files:", err)
		os.Exit(1)
	}
}

func run(ctx context.Context) error {
	m, err := newMatcher(ctx)
	if err != nil {
		return err
	}
	in := bufio.NewScanner(os.Stdin)
	out := bufio.NewWriter(os.Stdout)
	for in.Scan() {
		mod, path, ok := strings.Cut(in.Text(), "\t")
		if !ok {
			return fmt.Errorf("input line %q is not <module dir>\\t<path>", in.Text())
		}
		oses, err := m.legsFor(mod, path)
		if err != nil {
			return err
		}
		for _, goos := range oses {
			fmt.Fprintf(out, "%s\t%s\t%s\n", goos, mod, path)
		}
	}
	if err := in.Err(); err != nil {
		return err
	}
	return out.Flush()
}

func newMatcher(ctx context.Context) (*matcher, error) {
	self, err := os.Executable()
	if err != nil {
		return nil, err
	}
	m := &matcher{toolTags: map[target][]string{}, releaseTags: map[string][]string{}}
	for _, goos := range runnerOSes {
		for _, goarch := range goarches {
			tags, err := toolTags(ctx, self, goos, goarch)
			if err != nil {
				return nil, err
			}
			m.toolTags[target{goos, goarch}] = tags
		}
	}
	return m, nil
}

// toolTags asks a child of this binary for build.Default.ToolTags under
// GOOS/GOARCH: internal/buildcfg reads them, and GOAMD64/GOARM64 (unset, so the
// default level), from the environment at init, and drops the experiments a
// target lacks (dwarf5 on darwin, internal/buildcfg/exp.go).
func toolTags(ctx context.Context, self, goos, goarch string) ([]string, error) {
	cmd := exec.CommandContext(ctx, self, toolTagsFlag)
	for _, kv := range os.Environ() {
		switch k, _, _ := strings.Cut(kv, "="); k {
		case "GOOS", "GOARCH", "GOAMD64", "GOARM64":
		default:
			cmd.Env = append(cmd.Env, kv)
		}
	}
	cmd.Env = append(cmd.Env, "GOOS="+goos, "GOARCH="+goarch)
	cmd.Stderr = os.Stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("tool tags for %s/%s: %w", goos, goarch, err)
	}
	return strings.Fields(string(out)), nil
}

func (m *matcher) moduleReleaseTags(mod string) ([]string, error) {
	if tags, ok := m.releaseTags[mod]; ok {
		return tags, nil
	}
	data, err := os.ReadFile(filepath.Join(mod, "go.mod"))
	if err != nil {
		return nil, err
	}
	version := ""
	if v := toolchainLine.FindSubmatch(data); v != nil {
		version = string(v[1])
	} else if v := goLine.FindSubmatch(data); v != nil {
		version = string(v[1])
	}
	// With no version line setup-go installs nothing, and the runner's own Go is
	// unknown here.
	tags := build.Default.ReleaseTags
	if version != "" {
		tags, err = releaseTagsFor(version)
		if err != nil {
			return nil, fmt.Errorf("%s/go.mod: %w", mod, err)
		}
	}
	m.releaseTags[mod] = tags
	return tags, nil
}

// releaseTagsFor returns go1.1 through go1.N for Go 1.N, a release candidate
// included (go help buildconstraint).
func releaseTagsFor(version string) ([]string, error) {
	major, rest, _ := strings.Cut(version, ".")
	if major != "1" {
		return nil, fmt.Errorf("go version %q is not Go 1", version)
	}
	minor := rest
	if i := strings.IndexFunc(rest, func(r rune) bool { return r < '0' || r > '9' }); i >= 0 {
		minor = rest[:i]
	}
	n := 0
	if minor != "" {
		var err error
		if n, err = strconv.Atoi(minor); err != nil {
			return nil, fmt.Errorf("go version %q: %w", version, err)
		}
	}
	tags := make([]string, 0, n)
	for i := 1; i <= n; i++ {
		tags = append(tags, fmt.Sprintf("go1.%d", i))
	}
	return tags, nil
}

func (m *matcher) legsFor(mod, path string) ([]string, error) {
	release, err := m.moduleReleaseTags(mod)
	if err != nil {
		return nil, err
	}
	dir, name := filepath.Dir(path), filepath.Base(path)
	found := map[string]bool{}
	for _, goarch := range goarches {
		builds := map[string]bool{}
		excluded := false
		for _, goos := range runnerOSes {
			c := build.Default
			c.GOOS, c.GOARCH = goos, goarch
			// go-ci.yaml and go-os-ci.yaml run the race tests with CGO_ENABLED=1.
			c.CgoEnabled = true
			c.ToolTags = m.toolTags[target{goos, goarch}]
			c.ReleaseTags = release
			ok, err := c.MatchFile(dir, name)
			if err != nil {
				return nil, err
			}
			builds[goos] = ok
			excluded = excluded || !ok
		}
		for _, leg := range legs {
			found[leg] = found[leg] || (builds[leg] && excluded)
		}
	}
	var oses []string
	for _, leg := range legs {
		if found[leg] {
			oses = append(oses, leg)
		}
	}
	return oses, nil
}
