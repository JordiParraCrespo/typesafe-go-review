// Command slicer walks a Go package and emits review slices as JSON: one
// record per transaction struct with its fields, the source of its Validate,
// Flatten and TxType methods, its test function bodies, plus the package's
// sentinel errors. It is the discovery stage of the review pipeline.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
)

// Field is one struct field of a transaction type.
type Field struct {
	Name     string `json:"name"`
	Type     string `json:"type"`
	Optional bool   `json:"optional"`
	Doc      string `json:"doc,omitempty"`
}

// Slice is one reviewable transaction type and everything a question about it may need.
type Slice struct {
	Type     string            `json:"type"`
	File     string            `json:"file"`
	Line     int               `json:"line"`
	Fields   []Field           `json:"fields"`
	Methods  map[string]string `json:"methods"`
	Tests    map[string]string `json:"tests"`
	Sentinel []string          `json:"sentinels_returned"`
}

// Sentinel is one package-level ErrXxx variable.
type Sentinel struct {
	Name    string `json:"name"`
	Message string `json:"message"`
	File    string `json:"file"`
	Line    int    `json:"line"`
}

// Output is the whole slicer result for one package.
type Output struct {
	Package   string     `json:"package"`
	Slices    []Slice    `json:"slices"`
	Sentinels []Sentinel `json:"sentinels"`
}

func main() {
	dir := flag.String("dir", ".", "package directory to slice")
	embed := flag.String("embeds", "BaseTx", "only structs embedding this type")
	flag.Parse()

	fset := token.NewFileSet()
	pkgs, err := parser.ParseDir(fset, *dir, nil, parser.ParseComments)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	src := map[string][]byte{}
	read := func(path string) []byte {
		if b, ok := src[path]; ok {
			return b
		}
		b, _ := os.ReadFile(path)
		src[path] = b
		return b
	}
	text := func(n ast.Node) string {
		p := fset.Position(n.Pos())
		e := fset.Position(n.End())
		b := read(p.Filename)
		return string(b[p.Offset:e.Offset])
	}

	out := Output{}
	byType := map[string]*Slice{}
	for name, pkg := range pkgs {
		if strings.HasSuffix(name, "_test") {
			continue
		}
		out.Package = name
		// pass 1: structs and sentinels
		for path, f := range pkg.Files {
			base := filepath.Base(path)
			for _, d := range f.Decls {
				gd, ok := d.(*ast.GenDecl)
				if !ok {
					continue
				}
				for _, sp := range gd.Specs {
					switch s := sp.(type) {
					case *ast.TypeSpec:
						st, ok := s.Type.(*ast.StructType)
						if !ok || !embeds(st, *embed) {
							continue
						}
						sl := &Slice{Type: s.Name.Name, File: base, Line: fset.Position(s.Pos()).Line,
							Methods: map[string]string{}, Tests: map[string]string{}}
						for _, fl := range st.Fields.List {
							if len(fl.Names) == 0 {
								continue
							}
							ty := text(fl.Type)
							opt := strings.HasPrefix(ty, "*") || (fl.Tag != nil && strings.Contains(fl.Tag.Value, "omitempty"))
							doc := ""
							if fl.Doc != nil {
								doc = strings.TrimSpace(fl.Doc.Text())
							}
							sl.Fields = append(sl.Fields, Field{Name: fl.Names[0].Name, Type: ty, Optional: opt, Doc: doc})
						}
						byType[s.Name.Name] = sl
					case *ast.ValueSpec:
						if gd.Tok != token.VAR || base != "errors.go" {
							continue
						}
						for i, n := range s.Names {
							if !strings.HasPrefix(n.Name, "Err") || i >= len(s.Values) {
								continue
							}
							msg := ""
							if call, ok := s.Values[i].(*ast.CallExpr); ok && len(call.Args) > 0 {
								if lit, ok := call.Args[0].(*ast.BasicLit); ok {
									msg = strings.Trim(lit.Value, "\"")
								}
							}
							out.Sentinels = append(out.Sentinels, Sentinel{Name: n.Name, Message: msg, File: base, Line: fset.Position(n.Pos()).Line})
						}
					}
				}
			}
		}
		// pass 2: methods
		for _, f := range pkg.Files {
			for _, d := range f.Decls {
				fd, ok := d.(*ast.FuncDecl)
				if !ok || fd.Recv == nil || len(fd.Recv.List) == 0 {
					continue
				}
				rt := fd.Recv.List[0].Type
				if star, ok := rt.(*ast.StarExpr); ok {
					rt = star.X
				}
				id, ok := rt.(*ast.Ident)
				if !ok {
					continue
				}
				sl, ok := byType[id.Name]
				if !ok {
					continue
				}
				sl.Methods[fd.Name.Name] = text(fd)
				if fd.Name.Name == "Validate" {
					ast.Inspect(fd.Body, func(n ast.Node) bool {
						if id, ok := n.(*ast.Ident); ok && isSentinelName(id.Name) {
							sl.Sentinel = appendUnique(sl.Sentinel, id.Name)
						}
						return true
					})
				}
			}
		}
	}
	// pass 3: tests (same dir, _test files are parsed into the same package name for in-package tests)
	for name, pkg := range pkgs {
		_ = name
		for path, f := range pkg.Files {
			if !strings.HasSuffix(path, "_test.go") {
				continue
			}
			for _, d := range f.Decls {
				fd, ok := d.(*ast.FuncDecl)
				if !ok || !strings.HasPrefix(fd.Name.Name, "Test") {
					continue
				}
				// Assign the test to the longest type name it starts with, so
				// TestAMMCreateValidate (no underscore) still maps to AMMCreate
				// and TestPaymentChannelCreate_X never maps to Payment.
				best := ""
				for ty := range byType {
					if strings.HasPrefix(fd.Name.Name, "Test"+ty) && len(ty) > len(best) {
						best = ty
					}
				}
				if best != "" {
					byType[best].Tests[fd.Name.Name] = text(fd)
				}
			}
		}
	}
	for _, sl := range byType {
		out.Slices = append(out.Slices, *sl)
	}
	enc := json.NewEncoder(os.Stdout)
	if err := enc.Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func embeds(st *ast.StructType, name string) bool {
	for _, f := range st.Fields.List {
		if len(f.Names) == 0 {
			if id, ok := f.Type.(*ast.Ident); ok && id.Name == name {
				return true
			}
		}
	}
	return false
}

// isSentinelName reports whether name looks like a package sentinel error
// (ErrXxx). It excludes fmt.Errorf and similar identifiers.
func isSentinelName(name string) bool {
	return len(name) > 3 && strings.HasPrefix(name, "Err") && name[3] >= 'A' && name[3] <= 'Z'
}

func appendUnique(s []string, v string) []string {
	for _, x := range s {
		if x == v {
			return s
		}
	}
	return append(s, v)
}
