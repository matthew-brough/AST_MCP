#!/usr/bin/env bash
source ./lib.sh

# Greets the user.
greet() {
  echo "hi $1"
}

export APP_ENV=prod
