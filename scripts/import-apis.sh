#!/bin/bash
# This script imports API specifications into Azure API Management (APIM) using the Azure CLI.
# It handles version sets and allows for parallel processing of multiple API files.
# It also includes retry logic for failed imports and can process all APIs or only changed APIs based on the input mode.
set -e

# Environment variables (from GitHub secrets)
RESOURCE_GROUP="${RESOURCE_GROUP}"
APIM_INSTANCE="${APIM_INSTANCE}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID}"
MAX_CONCURRENT=4
MAX_RETRIES=3

# Check if we need to run for all APIs or just changed APIs
MODE="${MODE:-all}"  # Default to 'all' if not specified

# Function to check if version set exists
check_version_set() {
  local api_path=$1
  
  echo "Checking if version set exists for $api_path..."
  az apim api versionset show \
    --resource-group "$RESOURCE_GROUP" \
    --service-name "$APIM_INSTANCE" \
    --version-set-id "$api_path" \
    --output none 2>/dev/null

  return $?
}

# Function to create API version set
create_version_set() {
  local api_path=$1
  
  echo "Creating version set for $api_path..."
  # Using a single line command to avoid any issues with line continuations
  az apim api versionset create --resource-group "$RESOURCE_GROUP" --service-name "$APIM_INSTANCE" --version-set-id "$api_path" --display-name "$api_path" --versioning-scheme "header" --version-header-name "X-API-VERSION"
  
  if [ $? -eq 0 ]; then
    echo "Successfully created version set for $api_path"
    return 0
  else
    echo "Failed to create version set for $api_path"
    return 1
  fi
}

# Function to import API with version set
import_api() {
  local api_id=$1
  local api_version=$2
  local api_path=$3
  local version_set_id=$4
  local spec_path=$5
  local result_file=$6
  
  echo "Importing API $api_id with version $api_version..."
  
  # Try import with retry logic
  local retry_count=0
  local success=false
  
  while [ $retry_count -lt $MAX_RETRIES ] && [ "$success" = false ]; do
    echo "Attempt $((retry_count+1)) of $MAX_RETRIES"
    
    # Use az apim api import command
    az apim api import \
      --resource-group "$RESOURCE_GROUP" \
      --service-name "$APIM_INSTANCE" \
      --path "$api_path" \
      --api-id "$api_id" \
      --specification-path "$spec_path" \
      --specification-format OpenApi \
      --api-type http \
      --protocols https
      
    if [ $? -eq 0 ]; then
      success=true
      echo "Successfully imported $api_id"
      
      # Set API version and version set
      az apim api update \
        --resource-group "$RESOURCE_GROUP" \
        --service-name "$APIM_INSTANCE" \
        --api-id "$api_id" \
        --api-version "$api_version" \
        --api-version-set-id "$version_set_id"
        
      if [ $? -eq 0 ]; then
        echo "Successfully updated version info for $api_id"
        echo "{\"$api_id\": 200}" >> "$result_file"
      else
        echo "Failed to update version info for $api_id"
        echo "{\"$api_id\": 500}" >> "$result_file"
      fi
    else
      retry_count=$((retry_count+1))
      if [ $retry_count -lt $MAX_RETRIES ]; then
        echo "Import failed, retrying in 10 seconds..."
        sleep 10
      else
        echo "Failed to import $api_id after $MAX_RETRIES attempts"
        echo "{\"$api_id\": 400}" >> "$result_file"
      fi
    fi
  done
}

# Function to process a single API file
process_api_file() {
  local file=$1
  local result_file=$2
  
  # Extract file name without path and extension
  local filename=$(basename "$file")
  local baseName=$(basename "$filename" .yaml)
  
  # Get info using yq
  local serviceUrl=$(yq e '.servers[0].url' "$file")
  local versionId=$(yq e '.info.version' "$file")
  local displayName="$baseName-$versionId"
  
  # Get API name from the file name or directory structure
  local api_name="$baseName"
  local api_id="$api_name"
  local api_path="$api_name"
  local version_set_id="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.ApiManagement/service/$APIM_INSTANCE/apiVersionSets/$api_path"
  
  echo "Processing API: $api_name (version $versionId)"
  
  # Check and create version set if needed
  if ! check_version_set "$api_path"; then
    create_version_set "$api_path"
  fi
  
  # Import API
  import_api "$api_id" "$versionId" "$api_path" "$version_set_id" "$file" "$result_file"
}

# Function to run import jobs in parallel
run_parallel() {
  local -n jobs=$1
  local max_jobs=$2
  
  if [ ${#jobs[@]} -eq 0 ]; then
    return
  fi
  
  # Run initial batch of jobs
  local running=0
  local pids=()
  local job_indices=()
  
  for i in "${!jobs[@]}"; do
    if [ $running -ge $max_jobs ]; then
      break
    fi
    
    eval "${jobs[$i]}" &
    pids+=($!)
    job_indices+=($i)
    running=$((running + 1))
  done
  
  # Remove started jobs from the queue
  for i in $(seq $((running - 1)) -1 0); do
    unset "jobs[${job_indices[$i]}]"
  done
  
  # Wait for each job and start new ones as they complete
  for pid in "${pids[@]}"; do
    wait $pid
    
    # If there are more jobs, start one
    if [ ${#jobs[@]} -gt 0 ]; then
      local next_index=${!jobs[@]}
      eval "${jobs[$next_index]}" &
      local new_pid=$!
      
      # Replace the finished pid with the new one
      for i in "${!pids[@]}"; do
        if [ "${pids[$i]}" = "$pid" ]; then
          pids[$i]=$new_pid
          break
        fi
      done
      
      unset "jobs[$next_index]"
    fi
  done
  
  # Wait for any remaining jobs
  wait
}

# Main execution

# Create temp directory for results
TEMP_DIR=$(mktemp -d)
RESULT_FILE="$TEMP_DIR/results.json"
touch "$RESULT_FILE"

# Find API files
if [ "$MODE" = "all" ]; then
  # Process all yaml files in the apis directory (including subdirectories)
  echo "Processing all API files..."
  API_FILES=$(find ./apis -name "*.yaml" -type f)
else
  # Process only changed files from the last commit
  echo "Processing changed API files from the last commit..."
  CHANGED_FILES=$(git diff-tree --no-commit-id --name-only -r HEAD)
  API_FILES=$(echo "$CHANGED_FILES" | grep -E "^apis/.*\.yaml$" || echo "")
fi

if [ -z "$API_FILES" ]; then
  echo "No API files found to process."
  exit 0
fi

# Prepare jobs for parallel processing
declare -A IMPORT_JOBS
job_id=0

for file in $API_FILES; do
  # Skip files that don't exist (in case of deletions)
  if [ ! -f "$file" ]; then
    continue
  fi
  
  IMPORT_JOBS[$job_id]="process_api_file \"$file\" \"$RESULT_FILE\""
  job_id=$((job_id + 1))
done

# Run import jobs in parallel
echo "Processing ${#IMPORT_JOBS[@]} API imports (concurrency: $MAX_CONCURRENT)..."
run_parallel IMPORT_JOBS $MAX_CONCURRENT

echo "All API imports have completed."
echo "Results saved to: $RESULT_FILE"

# Display summary of results
echo "Summary of import results:"
cat "$RESULT_FILE" | jq -s 'add' 2>/dev/null || echo "No results recorded."

exit 0
