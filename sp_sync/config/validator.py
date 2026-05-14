"""
Configuration Validator
========================
Validates SharePoint and Google Drive configurations for sync operations.
Ensures paths exist, URLs are valid, and configurations are complete.
"""

import os
import re


class ConfigValidator:
    """Validates sync configurations."""
    
    @staticmethod
    def validate_sharepoint_config(config):
        """Validate a single SharePoint configuration."""
        errors = []
        warnings = []
        
        # Check required fields
        required_fields = ['name', 'rel_url', 'local_path']
        for field in required_fields:
            if field not in config or not config[field]:
                errors.append(f"Field '{field}' is required")
        
        if errors:
            return {'valid': False, 'errors': errors, 'warnings': warnings}
        
        # entry_type: folder (default) or file
        raw_et = config.get('entry_type')
        if raw_et is not None and str(raw_et).strip() != '':
            et = str(raw_et).strip().lower()
            if et not in ('folder', 'file'):
                errors.append("'entry_type' must be 'folder' or 'file'")
        
        # Validate name
        name = config['name']
        if len(name) < 3:
            warnings.append("Folder name is very short")
        
        # Validate relative URL
        rel_url = config['rel_url']
        if not rel_url.startswith('/'):
            errors.append("rel_url must start with '/'")
        elif 'personal' not in rel_url:
            warnings.append("rel_url does not contain 'personal' — may be wrong")
        
        # Validate local path (relative paths are normalized in fix_common_issues / save)
        local_path = os.path.abspath(
            os.path.expanduser(str(config['local_path']).strip())
        )
        if not os.path.isabs(local_path):
            errors.append("local_path is not valid")
        else:
            # Check if parent directory exists
            parent_dir = os.path.dirname(local_path)
            if not os.path.exists(parent_dir):
                warnings.append(f"Parent directory does not exist: {parent_dir}")
            elif not os.access(parent_dir, os.W_OK):
                errors.append(f"No write permission for: {parent_dir}")
        
        # Check for Arabic characters in path (Windows compatibility)
        if any(ord(c) > 127 for c in local_path) and os.name == 'nt':
            warnings.append("Path contains non-ASCII characters — may cause issues on some systems")
        
        return {
            'valid': len(errors) == 0,
            'errors': errors,
            'warnings': warnings
        }
    
    @staticmethod
    def validate_gdrive_config(config):
        """Validate a single Google Drive configuration."""
        errors = []
        warnings = []
        
        # Check required fields
        required_fields = ['name', 'folder_url', 'folder_id', 'local_path']
        for field in required_fields:
            if field not in config or not config[field]:
                errors.append(f"Field '{field}' is required")
        
        if errors:
            return {'valid': False, 'errors': errors, 'warnings': warnings}
        
        # Validate name
        name = config['name']
        if len(name) < 3:
            warnings.append("Folder name is very short")
        
        # Validate folder URL
        folder_url = config['folder_url']
        url_pattern = r'https://drive\.google\.com/drive/folders/[a-zA-Z0-9_-]+'
        if not re.match(url_pattern, folder_url):
            errors.append("Google Drive URL is invalid")
        
        # Validate folder ID
        folder_id = config['folder_id']
        if not re.match(r'^[a-zA-Z0-9_-]+$', folder_id):
            errors.append("folder_id is invalid")
        
        # Validate local path (relative paths are normalized in fix_common_issues / save)
        local_path = os.path.abspath(
            os.path.expanduser(str(config['local_path']).strip())
        )
        if not os.path.isabs(local_path):
            errors.append("local_path is not valid")
        else:
            # Check if parent directory exists
            parent_dir = os.path.dirname(local_path)
            if not os.path.exists(parent_dir):
                warnings.append(f"Parent directory does not exist: {parent_dir}")
            elif not os.access(parent_dir, os.W_OK):
                errors.append(f"No write permission for: {parent_dir}")
        
        return {
            'valid': len(errors) == 0,
            'errors': errors,
            'warnings': warnings
        }
    
    @staticmethod
    def validate_all_configs(sp_configs, gdrive_configs):
        """Validate all configurations."""
        results = {
            'sharepoint': {'configs': [], 'total_errors': 0, 'total_warnings': 0},
            'gdrive': {'configs': [], 'total_errors': 0, 'total_warnings': 0}
        }
        
        # Validate SharePoint configs
        for i, config in enumerate(sp_configs):
            validation = ConfigValidator.validate_sharepoint_config(config)
            validation['index'] = i
            validation['config'] = config
            results['sharepoint']['configs'].append(validation)
            results['sharepoint']['total_errors'] += len(validation['errors'])
            results['sharepoint']['total_warnings'] += len(validation['warnings'])
        
        # Validate Google Drive configs
        for i, config in enumerate(gdrive_configs):
            validation = ConfigValidator.validate_gdrive_config(config)
            validation['index'] = i
            validation['config'] = config
            results['gdrive']['configs'].append(validation)
            results['gdrive']['total_errors'] += len(validation['errors'])
            results['gdrive']['total_warnings'] += len(validation['warnings'])
        
        results['overall_valid'] = (
            results['sharepoint']['total_errors'] == 0 and 
            results['gdrive']['total_errors'] == 0
        )
        
        return results
    
    @staticmethod
    def fix_common_issues(config, config_type='sharepoint'):
        """Attempt to fix common configuration issues."""
        if config_type == 'sharepoint':
            raw_et = config.get('entry_type')
            if raw_et is None or str(raw_et).strip() == '':
                config['entry_type'] = 'folder'
            else:
                et = str(raw_et).strip().lower()
                config['entry_type'] = et if et in ('folder', 'file') else 'folder'
            if config.get('entry_type') == 'file':
                config.pop('local_flat', None)
            lf = config.get('local_flat')
            if lf is not None and str(lf).strip() != '':
                if isinstance(lf, str):
                    config['local_flat'] = lf.strip().lower() in ('1', 'true', 'yes')
                else:
                    config['local_flat'] = bool(lf)
            elif 'local_flat' in config and (config.get('local_flat') is None or str(config.get('local_flat')).strip() == ''):
                config.pop('local_flat', None)
            if 'local_path' in config and config['local_path']:
                config['local_path'] = os.path.abspath(
                    os.path.expanduser(str(config['local_path']).strip())
                )
            # Fix missing slashes in rel_url
            if 'rel_url' in config and config['rel_url']:
                rel_url = config['rel_url']
                if not rel_url.startswith('/'):
                    rel_url = '/' + rel_url
                config['rel_url'] = rel_url
            
            # Create local directory: full tree for folder; parent only for single file
            if 'local_path' in config and config['local_path']:
                local_path = config['local_path']
                try:
                    if config.get('entry_type') == 'file':
                        parent = os.path.dirname(local_path)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                    else:
                        os.makedirs(local_path, exist_ok=True)
                except Exception:
                    pass  # Silently fail if we can't create directory
        
        elif config_type == 'gdrive':
            if 'local_path' in config and config['local_path']:
                config['local_path'] = os.path.abspath(
                    os.path.expanduser(str(config['local_path']).strip())
                )
            # Extract folder_id from URL if missing
            if 'folder_url' in config and config['folder_url'] and not config.get('folder_id'):
                match = re.search(r'folders/([a-zA-Z0-9_-]+)', config['folder_url'])
                if match:
                    config['folder_id'] = match.group(1)
            
            # Create local directory if it doesn't exist
            if 'local_path' in config and config['local_path']:
                local_path = config['local_path']
                try:
                    os.makedirs(local_path, exist_ok=True)
                except Exception:
                    pass
        
        return config


def validate_and_fix_configs(store=None):
    """Validate configurations loaded from the SQLite app store."""
    from sp_sync.db.store import get_store

    store = store or get_store()
    sp_configs = store.get_sharepoint_configs()
    gdrive_configs = store.get_gdrive_configs()
    results = {
        "sharepoint": {
            "storage": "sqlite",
            "loaded": True,
            "configs": sp_configs,
            "validation": None,
        },
        "gdrive": {
            "storage": "sqlite",
            "loaded": True,
            "configs": gdrive_configs,
            "validation": None,
        },
    }
    results["validation"] = ConfigValidator.validate_all_configs(sp_configs, gdrive_configs)
    return results
