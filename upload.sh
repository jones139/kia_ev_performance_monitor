#!/bin/sh

target_folder=/home/graham/serverCfg/webApi/api/static/pv5

/home/graham/kia/venv/bin/python /home/graham/kia/publish.py 

scp publish/*  osdapi.org.uk:$target_folder



