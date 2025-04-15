import os
import sys
import json
import glob
import tempfile
import subprocess
import time
import yaml
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Environment variables (from GitHub secrets)
RESOURCE_GROUP = os.environ.get("RESOURCE_GROUP")
APIM_INSTANCE = os.environ.get("APIM_INSTANCE")
SUBSCRIPTION_ID = os.environ.get("SUBSCRIPTION_ID")
MAX_CONCURRENT = 4
MAX_RETRIES = 3

# Check if we need to run for all APIs or just changed APIs
MODE = os.environ.get("MODE", "all")  # Default to 'all' if not specified

# Azure API version
AZURE_API_VERSION = "2021-08-01"


def run_command(cmd, capture_output=True):
    """Run a shell command and return the result."""
    logger.debug(f"Running command: {cmd}")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=False,
            text=True,
            capture_output=capture_output
        )
        return result
    except Exception as e:
        logger.error(f"Error running command: {e}")
        raise


def get_access_token():
    """Get Azure access token using CLI."""
    cmd = "az account get-access-token --resource=https://management.azure.com/ --query accessToken -o tsv"
    result = run_command(cmd)
    if result.returncode == 0:
        return result.stdout.strip()
    else:
        logger.error(f"Failed to get access token: {result.stderr}")
        sys.exit(1)


def check_version_set(api_path):
    """Check if version set exists."""
    logger.info(f"Checking if version set exists for {api_path}...")
    cmd = (
        f"az apim api versionset show "
        f"--resource-group \"{RESOURCE_GROUP}\" "
        f"--service-name \"{APIM_INSTANCE}\" "
        f"--version-set-id \"{api_path}\" "
        f"--output none"
    )
    result = run_command(cmd)
    return result.returncode == 0


def create_version_set(api_path):
    """Create API version set using direct REST API call."""
    logger.info(f"Creating version set for {api_path} using REST API...")
    
    # Get access token
    token = get_access_token()
    
    # Create version set using REST API
    url = f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.ApiManagement/service/{APIM_INSTANCE}/apiVersionSets/{api_path}?api-version={AZURE_API_VERSION}"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    data = {
        "properties": {
            "displayName": api_path,
            "versioningScheme": "Header",
            "versionHeaderName": "X-API-VERSION"
        }
    }
    
    response = requests.put(url, headers=headers, json=data)
    
    if response.status_code in (200, 201):
        logger.info(f"Successfully created version set for {api_path}")
        return True
    else:
        logger.error(f"Failed to create version set for {api_path}: {response.text}")
        return False


def import_api(api_id, api_version, api_path, version_set_id, spec_path, result_file):
    """Import API with version set."""
    logger.info(f"Importing API {api_id} with version {api_version}...")
    
    # Try import with retry logic
    retry_count = 0
    success = False
    
    while retry_count < MAX_RETRIES and not success:
        logger.info(f"Attempt {retry_count + 1} of {MAX_RETRIES}")
        
        # Use az apim api import command
        import_cmd = (
            f"az apim api import "
            f"--resource-group \"{RESOURCE_GROUP}\" "
            f"--service-name \"{APIM_INSTANCE}\" "
            f"--path \"{api_path}\" "
            f"--api-id \"{api_id}\" "
            f"--specification-path \"{spec_path}\" "
            f"--specification-format OpenApi "
            f"--api-type http "
            f"--protocols https"
        )
        
        import_result = run_command(import_cmd)
        
        if import_result.returncode == 0:
            success = True
            logger.info(f"Successfully imported {api_id}")
            
            # Set API version and version set
            update_cmd = (
                f"az apim api update "
                f"--resource-group \"{RESOURCE_GROUP}\" "
                f"--service-name \"{APIM_INSTANCE}\" "
                f"--api-id \"{api_id}\" "
                f"--api-version \"{api_version}\" "
                f"--api-version-set-id \"{version_set_id}\""
            )
            
            update_result = run_command(update_cmd)
            
            if update_result.returncode == 0:
                logger.info(f"Successfully updated version info for {api_id}")
                with open(result_file, 'a') as f:
                    f.write(json.dumps({api_id: 200}) + "\n")
            else:
                logger.error(f"Failed to update version info for {api_id}: {update_result.stderr}")
                with open(result_file, 'a') as f:
                    f.write(json.dumps({api_id: 500}) + "\n")
        else:
            retry_count += 1
            if retry_count < MAX_RETRIES:
                logger.warning(f"Import failed, retrying in 10 seconds... Error: {import_result.stderr}")
                time.sleep(10)
            else:
                logger.error(f"Failed to import {api_id} after {MAX_RETRIES} attempts: {import_result.stderr}")
                with open(result_file, 'a') as f:
                    f.write(json.dumps({api_id: 400}) + "\n")


def process_api_file(file, result_file):
    """Process a single API file."""
    # Extract file name without path and extension
    filename = os.path.basename(file)
    base_name = os.path.splitext(filename)[0]
    
    # Get info from YAML file
    try:
        with open(file, 'r') as f:
            api_spec = yaml.safe_load(f)
        
        service_url = api_spec.get('servers', [{}])[0].get('url', '')
        version_id = api_spec.get('info', {}).get('version', '1.0')
        display_name = f"{base_name}-{version_id}"
        
        # Get API name from the file name or directory structure
        api_name = base_name
        api_id = api_name
        api_path = api_name
        version_set_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.ApiManagement/service/{APIM_INSTANCE}/apiVersionSets/{api_path}"
        
        logger.info(f"Processing API: {api_name} (version {version_id})")
        
        # Check and create version set if needed
        if not check_version_set(api_path):
            if not create_version_set(api_path):
                logger.error(f"Failed to create version set for {api_path}, skipping API import")
                with open(result_file, 'a') as f:
                    f.write(json.dumps({api_id: 500}) + "\n")
                return
        
        # Import API
        import_api(api_id, version_id, api_path, version_set_id, file, result_file)
        
    except Exception as e:
        logger.error(f"Error processing API file {file}: {e}")
        with open(result_file, 'a') as f:
            f.write(json.dumps({base_name: 500}) + "\n")


def main():
    """Main execution function."""
    # Create temp directory for results
    temp_dir = tempfile.mkdtemp()
    result_file = os.path.join(temp_dir, "results.json")
    
    # Find API files
    if MODE == "all":
        # Process all yaml files in the apis directory (including subdirectories)
        logger.info("Processing all API files...")
        api_files = glob.glob("./apis/**/*.yaml", recursive=True)
    else:
        # Process only changed files from the last commit
        logger.info("Processing changed API files from the last commit...")
        changed_files_cmd = "git diff-tree --no-commit-id --name-only -r HEAD"
        result = run_command(changed_files_cmd)
        changed_files = result.stdout.strip().split('\n')
        api_files = [f for f in changed_files if f.startswith("apis/") and f.endswith(".yaml")]
    
    if not api_files:
        logger.info("No API files found to process.")
        return 0
    
    # Process API files in parallel
    logger.info(f"Processing {len(api_files)} API imports (concurrency: {MAX_CONCURRENT})...")
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
        futures = []
        for file in api_files:
            if os.path.isfile(file):
                futures.append(executor.submit(process_api_file, file, result_file))
        
        # Wait for all tasks to complete
        for future in futures:
            try:
                future.result()
            except Exception as e:
                logger.error(f"Error in worker thread: {e}")
    
    logger.info("All API imports have completed.")
    logger.info(f"Results saved to: {result_file}")
    
    # Display summary of results
    logger.info("Summary of import results:")
    try:
        results = {}
        with open(result_file, 'r') as f:
            for line in f:
                try:
                    result_dict = json.loads(line)
                    results.update(result_dict)
                except json.JSONDecodeError:
                    pass
        
        print(json.dumps(results, indent=2))
    except Exception as e:
        logger.error(f"Error reading results: {e}")
        print("No results recorded.")
    
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Operation cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        sys.exit(1)
