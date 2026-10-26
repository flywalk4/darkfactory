#!/bin/sh
# No PyPI version check on every start: it delays the server past the agent CLI startup timeout.
FASTMCP_CHECK_FOR_UPDATES=off exec /opt/mcp-atlassian/bin/mcp-atlassian "$@"
