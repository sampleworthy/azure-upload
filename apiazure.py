import os
import requests
import time
import re
import sys

# ENV VARS set from the pipeline
clientId = os.getenv('clientId')
clientSecret = os.getenv('clientSecret')
resourceGroupName = os.getenv('resourceGroupName')
apimServiceName = os.getenv('apimServiceName')
tenantId = os.getenv('tenantId')
subscriptionId = os.getenv('subscriptionId')
resource = "https://management.azure.com/.default"
azureApiVersion = "2021-08-01"
baseUrl = f"https://management.azure.com/subscriptions/{subscriptionId}/resourceGroups/{resourceGroupName}/providers/Microsoft.ApiManagement/service/{apimServiceName}"

def getToken():
    url = f"https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token"
    data = {
        "client_id": clientId,
        "client_secret": clientSecret,
        "grant_type": "client_credentials",
        "scope": resource
    }

    r = requests.post(url, data=data)
    if r.status_code == 200:
        return r.json()['access_token']
    else:
        print(r.status_code)
        print(r.text)
        sys.exit(1)

def createOrUpdateVersionSet(apiPath):
    token = getToken()
    url = f"{baseUrl}/apiVersionSets/{apiPath}"
    params = {'api-version': azureApiVersion}
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'If-Match': '*'}
    data = {'properties': {"displayName": apiPath, "versioningScheme": "Header", "versionHeaderName": "X-API-VERSION"}}

    r = requests.put(url=url, params=params, headers=headers, json=data)
    if r.status_code in (200, 201):
        print(f"{r.status_code} Created Version Set {apiPath}")
    else: 
        print(f"{r.status_code} Error creating Version Set {apiPath}")

def main():
    regex = re.compile("^([a-zA-Z0-9_]*)-(v\d{0,3})\.yaml$")
    files = os.listdir('./openapi/')
    files = [file for file in files if regex.match(file)]
    if files:
        print("Checking Version Sets...")
        vSets = set(re.split('-|\.', file)[0] for file in files)
        for vSet in vSets:
            createOrUpdateVersionSet(vSet)
    else:
        print("Didn't find any spec files, exiting")
        sys.exit(1)

if __name__ == "__main__":
    main()
