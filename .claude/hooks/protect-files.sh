#!/bin/bash
# Block modifications to sensitive files (.env, secrets, credentials)
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# Block .env and secret files
if [[ "$FILE_PATH" == *".env"* ]] || \
   [[ "$FILE_PATH" == *"secret"* ]] || \
   [[ "$FILE_PATH" == *"credential"* ]] || \
   [[ "$FILE_PATH" == *"id_rsa"* ]]; then
  echo "Blocked: Cannot modify sensitive file: $FILE_PATH" >&2
  exit 2
fi

# Block dangerous bash commands
if [[ "$COMMAND" == *"rm -rf /"* ]] || \
   [[ "$COMMAND" == *"drop table"* ]] || \
   [[ "$COMMAND" == *"drop database"* ]] || \
   [[ "$COMMAND" == *"--force"* && "$COMMAND" == *"push"* ]] || \
   [[ "$COMMAND" == *"reset --hard"* ]]; then
  echo "Blocked: Dangerous command detected: $COMMAND" >&2
  exit 2
fi

exit 0
