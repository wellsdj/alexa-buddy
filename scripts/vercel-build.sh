#!/usr/bin/env bash
# Builds the Study Buddy website: the same pages Buddy's Mac serves, as the site's front page.
set -e
rm -rf public
mkdir -p public
cp server/web/study.html public/index.html
cp server/web/study.css server/web/study.js public/
