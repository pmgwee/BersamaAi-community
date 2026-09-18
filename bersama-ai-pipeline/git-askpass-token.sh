#!/usr/bin/env sh
# Git invokes this helper with a username/password prompt. The token stays in
# the process environment and never enters the remote URL or command line.
case "$1" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *Password*) printf '%s\n' "${GITHUB_TOKEN:?GITHUB_TOKEN is required}" ;;
  *) exit 1 ;;
esac
