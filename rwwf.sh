#!/usr/bin/env bash

kubectl apply -f ~/workflows/rw-user.yaml
sleep 1
ARGO_TOKEN="Bearer $(kubectl get secret -n argo argo-workflows-rw-user -o=jsonpath='{.data.token}' | base64 --decode)"
echo $ARGO_TOKEN
