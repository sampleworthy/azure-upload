#!/usr/bin/env python3
import sys
import os
import json
import yaml
import logging
import re
from pathlib import Path
import tempfile
import subprocess

logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('api-validator')

class ApiValidator:
    def __init__(self, spec_path):
        self.spec_path = spec_path
        self.errors = []
        self.warnings = []
        
        # Load the specification
        try:
            with open(spec_path, 'r') as f:
                content = f.read()
                if spec_path.endswith('.json'):
                    self.spec = json.loads(content)
                else:  # Assume YAML
                    self.spec = yaml.safe_load(content)
        except Exception as e:
            self.errors.append(f"Failed to load specification: {str(e)}")
            self.spec = None
    
    def validate(self):
        """Run all validation checks"""
        if not self.spec:
            return False
        
        # Core validation checks
        self.check_operation_ids()
        self.check_path_parameters()
        self.check_security_definitions()
        self.check_content_types()
        self.check_path_trailing_slashes()
        self.check_servers()
        self.check_response_definitions()
        self.check_ref_siblings()
        
        return len(self.errors) == 0
    
    def check_operation_ids(self):
        """Verify all operations have unique operationIds"""
        if not self.spec.get('paths'):
            self.errors.append("No paths defined in specification")
            return
        
        operation_ids = {}
        
        for path, path_item in self.spec['paths'].items():
            for method in ['get', 'post', 'put', 'delete', 'patch']:
                if method not in path_item:
                    continue
                    
                operation = path_item[method]
                if 'operationId' not in operation:
                    self.errors.append(f"Missing operationId in {method.upper()} {path}")
                else:
                    op_id = operation['operationId']
                    if op_id in operation_ids:
                        self.errors.append(f"Duplicate operationId '{op_id}' found in {method.upper()} {path} and {operation_ids[op_id]}")
                    else:
                        operation_ids[op_id] = f"{method.upper()} {path}"
    
    def check_path_parameters(self):
        """Verify path parameters are properly defined"""
        if not self.spec.get('paths'):
            return
            
        for path, path_item in self.spec['paths'].items():
            # Find parameters in path template
            path_params = re.findall(r'{([^}]+)}', path)
            
            for method in ['get', 'post', 'put', 'delete', 'patch']:
                if method not in path_item:
                    continue
                
                operation = path_item[method]
                operation_params = []
                
                # Check operation parameters
                if 'parameters' in operation:
                    for param in operation.get('parameters', []):
                        if param.get('in') == 'path':
                            operation_params.append(param.get('name'))
                            
                            # Ensure required=true for path parameters
                            if not param.get('required'):
                                self.errors.append(f"Path parameter '{param.get('name')}' in {method.upper()} {path} must have required=true")
                
                # Check path item parameters
                if 'parameters' in path_item:
                    for param in path_item.get('parameters', []):
                        if param.get('in') == 'path' and param.get('name') not in operation_params:
                            operation_params.append(param.get('name'))
                
                # Verify all path template parameters are defined
                for param_name in path_params:
                    if param_name not in operation_params:
                        self.errors.append(f"Path parameter '{{{param_name}}}' in {path} is not defined in {method.upper()} operation")
                
                # Verify no extra path parameters are defined
                for param_name in operation_params:
                    if param_name not in path_params:
                        self.errors.append(f"Defined path parameter '{param_name}' in {method.upper()} {path} not found in path template")
    
    def check_security_definitions(self):
        """Check security definitions for potential APIM issues"""
        if not self.spec.get('components') or not self.spec['components'].get('securitySchemes'):
            return
            
        for name, scheme in self.spec['components']['securitySchemes'].items():
            # Check for empty or none type
            if not scheme.get('type'):
                self.errors.append(f"Security scheme '{name}' is missing a type")
                
            # Check for APIM compatibility issues with certain security schemes
            if scheme.get('type') == 'oauth2':
                # Check for multiple flows
                if scheme.get('flows') and len(scheme['flows']) > 1:
                    self.warnings.append(f"Multiple OAuth2 flows defined in '{name}' may cause issues in APIM")
                    
                # Check for complex scopes
                for flow_name, flow in scheme.get('flows', {}).items():
                    if flow.get('scopes') and len(flow['scopes']) > 10:
                        self.warnings.append(f"Large number of scopes in OAuth2 flow '{flow_name}' may cause issues in APIM")
    
    def check_content_types(self):
        """Check for potentially problematic content types"""
        supported_types = [
            'application/json', 
            'application/xml', 
            'text/plain', 
            'multipart/form-data', 
            'application/x-www-form-urlencoded'
        ]
        
        if not self.spec.get('paths'):
            return
            
        for path, path_item in self.spec['paths'].items():
            for method in ['get', 'post', 'put', 'delete', 'patch']:
                if method not in path_item:
                    continue
                    
                operation = path_item[method]
                
                # Check request body content types
                if 'requestBody' in operation and 'content' in operation['requestBody']:
                    for content_type in operation['requestBody']['content'].keys():
                        if content_type not in supported_types:
                            self.warnings.append(f"Content type '{content_type}' in {method.upper()} {path} request body may not be well supported in APIM")
                
                # Check response content types
                if 'responses' in operation:
                    for status, response in operation['responses'].items():
                        if 'content' in response:
                            for content_type in response['content'].keys():
                                if content_type not in supported_types:
                                    self.warnings.append(f"Content type '{content_type}' in {method.upper()} {path} response may not be well supported in APIM")
    
    def check_path_trailing_slashes(self):
        """Check for trailing slashes in paths"""
        if not self.spec.get('paths'):
            return
            
        for path in self.spec['paths'].keys():
            if path != '/' and path.endswith('/'):
                self.errors.append(f"Path '{path}' ends with a trailing slash, which may cause issues in APIM")
    
    def check_servers(self):
        """Check for server information"""
        # For OpenAPI 3.0
        if self.spec.get('openapi', '').startswith('3.') and not self.spec.get('servers'):
            self.errors.append("No servers defined in OpenAPI 3.0 specification")
            
        # For OpenAPI 2.0 (Swagger)
        if self.spec.get('swagger', '').startswith('2.') and not (self.spec.get('host') or self.spec.get('basePath')):
            self.errors.append("No host or basePath defined in Swagger 2.0 specification")
    
    def check_response_definitions(self):
        """Check that operations have at least one success response"""
        if not self.spec.get('paths'):
            return
            
        for path, path_item in self.spec['paths'].items():
            for method in ['get', 'post', 'put', 'delete', 'patch']:
                if method not in path_item:
                    continue
                    
                operation = path_item[method]
                
                if 'responses' not in operation:
                    self.errors.append(f"No responses defined for {method.upper()} {path}")
                    continue
                    
                has_success = False
                for status in operation['responses'].keys():
                    if status.startswith('2') or status.startswith('3'):
                        has_success = True
                        
                        # Check for empty response definition
                        response = operation['responses'][status]
                        if not response or (isinstance(response, dict) and not response.get('description')):
                            self.errors.append(f"Empty success response definition for {method.upper()} {path} with status {status}")
                        
                if not has_success:
                    self.errors.append(f"No success response (2xx, 3xx) defined for {method.upper()} {path}")
    
    def check_ref_siblings(self):
        """Check for $ref with siblings which APIM doesn't allow"""
        def check_object(obj, path="root"):
            if isinstance(obj, dict):
                if '$ref' in obj and len(obj.keys()) > 1:
                    self.errors.append(f"Object at {path} has $ref with siblings, which APIM doesn't support")
                
                for key, value in obj.items():
                    check_object(value, f"{path}.{key}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    check_object(item, f"{path}[{i}]")
        
        check_object(self.spec)
    
    def run_spectral(self):
        """Run Spectral for additional validation if available"""
        try:
            # Check if spectral is installed
            result = subprocess.run(['spectral', '--version'], 
                                  capture_output=True, 
                                  text=True)
            
            # Run spectral against the spec
            result = subprocess.run(['spectral', 'lint', self.spec_path], 
                                  capture_output=True, 
                                  text=True)
            
            if result.returncode != 0:
                for line in result.stdout.splitlines():
                    if 'error' in line.lower():
                        self.errors.append(f"Spectral: {line.strip()}")
                    elif 'warning' in line.lower():
                        self.warnings.append(f"Spectral: {line.strip()}")
                        
            return True
        except Exception as e:
            logger.warning(f"Spectral validation not available: {str(e)}")
            return False
    
    def report(self):
        """Generate a validation report"""
        if not self.errors and not self.warnings:
            logger.info(f"✅ {self.spec_path} passed all validation checks")
            return True
            
        if self.errors:
            logger.error(f"❌ {self.spec_path} failed validation with {len(self.errors)} errors:")
            for i, error in enumerate(self.errors, 1):
                logger.error(f"  {i}. {error}")
                
        if self.warnings:
            logger.warning(f"⚠️ {self.spec_path} has {len(self.warnings)} warnings:")
            for i, warning in enumerate(self.warnings, 1):
                logger.warning(f"  {i}. {warning}")
                
        return len(self.errors) == 0


def validate_and_save_spectral_ruleset():
    """Create .spectral.yaml file with APIM-specific rules"""
    spectral_content = """
extends: spectral:oas
rules:
  # Common issues that cause APIM imports to fail
  operation-success-response:
    description: Operations should return at least one success response
    given: $.paths.*[get,post,put,delete,patch]
    severity: error
    then:
      field: responses
      function: schema
      functionOptions:
        schema:
          type: object
          patternProperties:
            "^(2[0-9][0-9]|3[0-9][0-9])$": {}
          minProperties: 1
          
  operation-operationId-unique:
    description: Every operation must have a unique operationId
    given: $.paths.*[get,post,put,delete,patch]
    severity: error
    then:
      field: operationId
      function: truthy
      
  operation-parameters-unique:
    description: Operation parameters must have unique name and location combination
    given: $.paths.*[get,post,put,delete,patch].parameters
    severity: error
    then:
      function: uniqueItems
      functionOptions:
        uniqueProperties:
          - name
          - in
          
  no-$ref-siblings:
    description: $ref objects cannot have siblings
    given: $..[$ref]
    severity: error
    then:
      function: refSiblings
      
  operation-parameters:
    description: Operation parameters must have a name and in property
    given: $.paths.*[get,post,put,delete,patch].parameters[*]
    severity: error
    then:
      field: name
      function: truthy
      
  schema-properties-defined:
    description: Schema properties must be defined as objects
    given: $.components.schemas.*.properties
    severity: error
    then:
      function: schema
      functionOptions:
        schema:
          type: object
          
  path-keys-no-trailing-slash:
    description: Path keys should not end with trailing slash
    given: $.paths.*~
    severity: error
    then:
      function: pattern
      functionOptions:
        notMatch: .+/$
        
  apim-specific-friendly-names:
    description: DisplayName should be present for operations (helps APIM)
    given: $.paths.*[get,post,put,delete,patch]
    severity: warn
    then:
      field: summary
      function: truthy

  api-host-defined:
    description: API must have a server defined
    given: $
    severity: error
    then:
      field: servers
      function: truthy

  apim-content-types:
    description: Content types should be supported by APIM
    given: $..content.*~
    severity: warn
    then:
      function: pattern
      functionOptions:
        match: ^(application/json|application/xml|text/plain|multipart/form-data|application/x-www-form-urlencoded)$
"""
    
    with open('.spectral.yaml', 'w') as f:
        f.write(spectral_content.strip())
    
    logger.info("Created .spectral.yaml with APIM-specific rules")


def main():
    if len(sys.argv) < 2:
        print("Usage: python api-validator.py <path-to-spec> [--all]")
        return 1
    
    # Check if we need to validate all specs
    if sys.argv[1] == '--all':
        specs = []
        for ext in ['.yaml', '.yml', '.json']:
            specs.extend(list(Path('./apis').glob(f'*{ext}')))
    else:
        specs = [Path(sys.argv[1])]
    
    # Create spectral ruleset
    validate_and_save_spectral_ruleset()
    
    # Validate each spec
    all_valid = True
    for spec_path in specs:
        validator = ApiValidator(str(spec_path))
        
        # Run validation
        is_valid = validator.validate()
        
        # Try to run spectral if available
        validator.run_spectral()
        
        # Show report
        spec_valid = validator.report()
        all_valid = all_valid and spec_valid
    
    return 0 if all_valid else 1


if __name__ == "__main__":
    sys.exit(main())
