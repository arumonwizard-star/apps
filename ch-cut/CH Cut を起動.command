#!/bin/bash
# CH Cut を起動する（ダブルクリック）。CH Cut.app が動かないときの予備。
cd "$(dirname "$0")"
exec "CH Cut.app/Contents/MacOS/CHCut"
