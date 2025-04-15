import os
import sys
import json
import glob
import tempfile
import subprocess
import time
import yaml
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import logging
from azure.identity import DefaultAzureCredential
from azure.mgmt.apimanagement import ApiManagementClient
from azure.core.exceptions import HttpResponseError

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


def get_apim_client():
    """Get an Azure API Management client."""
    credential = DefaultAzureCredential()
    client = ApiManagementClient(credential, SUBSCRIPTION_ID)
    return client


def check_version_set(api_path, client):
    """Check if version set exists using Azure SDK."""
    logger.info(f"Checking if version set exists for {api_path}...")
    try:
        version_set = client.api_version_set.get(
            resource_group_name=RESOURCE_GROUP,
            service_name=APIM_INSTANCE,
            version_set_id=api_path
        )
        logger.info(f"Version set {api_path} exists")
        return True
    except HttpResponseError as e:
        if e.status_code == 404:
            logger.info(f"Version set {api_path} does not exist")
            return False
        else:
            logger.error(f"Error checking version set {api_path}: {str(e)}")
            raise


def create_version_set(api_path, client):
    """Create API version set using Azure SDK."""
    logger.info(f"Creating version set for {api_path}...")
    try:
        version_set = client.api_version_set.create_or_update(
            resource_group_name=RESOURCE_GROUP,
            service_name=APIM_INSTANCE,
            version_set_id=api_path,
            parameters={
                "display_name": api_path,
                "versioning_scheme": "Header",
                "version_header_name": "X-API-VERSION"
            }
        )
        logger.info(f"Successfully created version set for {api_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to create version set for {api_path}: {str(e)}")
        return False


def import_api(api_id, api_version, api_path, version_set_id, spec_path, client, result_file):
    """Import API with version set using Azure CLI and update with SDK."""
    logger.info(f"Importing API {api_id} with version {api_version}...")
    
    # Try import with retry logic
    retry_count = 0
    success = False
    
    while retry_count < MAX_RETRIES and not success:
        logger.info(f"Attempt {retry_count + 1} of {MAX_RETRIES}")
        
        # Use az apim api import command (CLI is still best for import)
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
            
            # Update API with version info using SDK
            try:
                # Get current API
                current_api = client.api.get(
                    resource_group_name=RESOURCE_GROUP,
                    service_name=APIM_INSTANCE,
                    api_id=api_id
                )
                
                # Prepare update parameters
                update_params = {
                    "api_version": api_version,
                    "api_version_set_id": version_set_id,
                    "display_name": current_api.display_name,
                    "service_url": current_api.service_url,
                    "path": current_api.path,
                    "protocols": current_api.protocols
                }
                
                # Update API
                client.api.update(
                    resource_group_name=RESOURCE_GROUP,
                    service_name=APIM_INSTANCE,
                    api_id=api_id,
                    parameters=update_params
                )
                
                logger.info(f"Successfully updated version info for {api_id}")
                with open(result_file, 'a') as f:
                    f.write(json.dumps({api_id: 200}) + "\n")
            except Exception as e:
                logger.error(f"Failed to update version info for {api_id}: {str(e)}")
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


def process_api_file(file, client, result_file):
    """Process a single API file."""
    # Extract file name without path and extension
    filename = os.path.basename(file)
    base_name = os.path.splitext(filename)[0]
    
    # Get info from YAML file
    try:
        with open(file, 'r') as f:
            api_spec = yaml.safe_load(f)
        
        version_id = api_spec.get('info', {}).get('version', '1.0')
        
        # Get API name from the file name or directory structure
        api_name = base_name
        api_id = api_name
        api_path = api_name
        version_set_id = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.ApiManagement/service/{APIM_INSTANCE}/apiVersionSets/{api_path}"
        
        logger.info(f"Processing API: {api_name} (version {version_id})")
        
        # Check and create version set if needed
        if not check_version_set(api_path, client):
            if not create_version_set(api_path, client):
                logger.error(f"Failed to create version set for {api_path}, skipping API import")
                with open(result_file, 'a') as f:
                    f.write(json.dumps({api_id: 500}) + "\n")
                return
        
        # Import API
        import_api(api_id, version_id, api_path, version_set_id, file, client, result_file)
        
    except Exception as e:
        logger.error(f"Error processing API file {file}: {str(e)}")
        with open(result_file, 'a') as f:
            f.write(json.dumps({base_name: 500}) + "\n")


def main():
    """Main execution function."""
    # Create temp directory for results
    temp_dir = tempfile.mkdtemp()
    result_file = os.path.join(temp_dir, "results.json")
    
    # Get API Management client
    client = get_apim_client()
    
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
                futures.append(executor.submit(process_api_file, file, client, result_file))
        
        # Wait for all tasks to complete
        for future in futures:
            try:
                future.result()
            except Exception as e:
                logger.error(f"Error in worker thread: {str(e)}")
    
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
