package main

import (
	"fmt"
	alias "strings"
)

// Widget is a widget.
type Widget struct {
	Name string
}

// Renderer renders.
type Renderer interface {
	Render() string
}

// Version is the version.
const Version = "1.0"

// Render returns the name.
func (w *Widget) Render() string {
	return w.Name
}

// Build makes a widget.
func Build(name string) (*Widget, error) {
	return &Widget{Name: name}, fmt.Errorf("x %s", alias.ToUpper(name))
}
