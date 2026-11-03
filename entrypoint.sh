#!/bin/sh
set -e

# 取得サイクルのループは main.py 自身が持つため、このラッパーは1回起動するだけ。
exec python3 src/main.py
