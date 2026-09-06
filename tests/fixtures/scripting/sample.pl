package Widget;
use strict;
use POSIX;

# Builds a widget.
sub build {
    my ($name) = @_;
    return $name;
}

sub render { return 1; }
1;
