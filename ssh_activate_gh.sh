#!/bin/bash

### A script to add ssh-keys for github ssh access
### It checks for previous agents and removes them
### Adds the ssh-keys at the end
### run a source 'source ssh-activate.sh'

ssh_agent=$(ps -aux | grep "ssh-agent" | wc -l)
if [[ $ssh_agent -gt 0 ]]; then 
	PDD=$(ps -aux | grep $USER.*ssh-agent | cut -c 11-17) 
	echo "extra agents: $PDD, need to remove."
	echo "[ssh] Starting new ssh-agent..."
	eval $(ssh-agent -s | tee ~/.ssh/agent.info)
	trap "ssh-agent -k" EXIT
	echo "[ssh] Reusing existing ssh-agent (PID ${SSH_AGENT_PID:-unknown})"
	eval `ssh-agent -s`
fi
echo "Adding keys to agent $PDD"

ssh-add ~/.ssh/ed_lustre
ssh-add ~/.ssh/gh_tf
