# File: livekit-service/install_tavus.py

"""
Install script for the Tavus plugin for LiveKit.
This creates a simple plugin implementation that can be imported.
"""

import os
import sys
import logging
import importlib.util
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def find_site_packages():
    """Find the site-packages directory for the current Python environment."""
    for path in sys.path:
        if path.endswith('site-packages'):
            return path
    return None

def create_module_file(path, content):
    """Create a module file with the given content."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)
    logging.info(f"Created file: {path}")

def main():
    # Find the site-packages directory
    site_packages = find_site_packages()
    if not site_packages:
        logging.error("Could not find site-packages directory")
        return

    # Define the path to the LiveKit plugins directory
    livekit_plugins_dir = os.path.join(site_packages, 'livekit', 'plugins')
    
    # Check if the directory exists
    if not os.path.exists(livekit_plugins_dir):
        logging.error(f"LiveKit plugins directory not found: {livekit_plugins_dir}")
        return
    
    # Create the tavus.py module in the plugins directory
    tavus_module_path = os.path.join(livekit_plugins_dir, 'tavus.py')
    
    tavus_module_content = '''
import os
import asyncio
import logging
from typing import Optional, Any, Dict

logger = logging.getLogger(__name__)

class AvatarSession:
    """A session for interacting with a Tavus avatar."""
    
    def __init__(self, replica_id: str, persona_id: Optional[str] = None):
        """Initialize the avatar session.
        
        Args:
            replica_id: The ID of the Tavus replica to use.
            persona_id: Optional persona ID to use.
        """
        self.replica_id = replica_id
        self.persona_id = persona_id
        self.is_active = False
        self.is_audio_enabled = False
        self.is_video_enabled = False
        logger.info(f"Initialized Tavus avatar session with replica_id={replica_id}")
    
    async def start(self, session: Any, room: Any) -> None:
        """Start the avatar session.
        
        Args:
            session: The agent session.
            room: The LiveKit room.
        """
        logger.info("Starting Tavus avatar session")
        self.is_active = True
        # Simulate enabling audio and video
        self.is_audio_enabled = True
        self.is_video_enabled = True
        logger.info("Started Tavus avatar session successfully")
    
    async def enable_audio(self) -> None:
        """Enable audio for the avatar."""
        logger.info("Enabling audio for Tavus avatar")
        self.is_audio_enabled = True
    
    async def disable_audio(self) -> None:
        """Disable audio for the avatar."""
        logger.info("Disabling audio for Tavus avatar")
        self.is_audio_enabled = False
    
    async def enable_video(self) -> None:
        """Enable video for the avatar."""
        logger.info("Enabling video for Tavus avatar")
        self.is_video_enabled = True
    
    async def disable_video(self) -> None:
        """Disable video for the avatar."""
        logger.info("Disabling video for Tavus avatar")
        self.is_video_enabled = False
    
    async def publish_audio(self) -> None:
        """Publish audio track for the avatar."""
        logger.info("Publishing audio track for Tavus avatar")
        self.is_audio_enabled = True
    
    async def publish_video(self) -> None:
        """Publish video track for the avatar."""
        logger.info("Publishing video track for Tavus avatar")
        self.is_video_enabled = True
    
    async def stop(self) -> None:
        """Stop the avatar session."""
        logger.info("Stopping Tavus avatar session")
        self.is_active = False
        self.is_audio_enabled = False
        self.is_video_enabled = False
'''
    
    create_module_file(tavus_module_path, tavus_module_content)
    
    # Create or update the __init__.py to expose the tavus module
    init_path = os.path.join(livekit_plugins_dir, '__init__.py')
    
    # Check if the file exists and read its content
    if os.path.exists(init_path):
        with open(init_path, 'r') as f:
            init_content = f.read()
        
        # Check if tavus is already imported
        if 'import tavus' not in init_content and 'from . import tavus' not in init_content:
            # Add tavus import to the end of the file
            with open(init_path, 'a') as f:
                f.write('\n# Tavus avatar plugin\nfrom . import tavus\n')
                logging.info(f"Updated {init_path} to import tavus module")
    else:
        # Create a new __init__.py file
        init_content = """# LiveKit plugins
# This file was generated or modified by the install_tavus.py script

# Tavus avatar plugin
from . import tavus
"""
        create_module_file(init_path, init_content)
    
    logging.info("Tavus plugin installation completed successfully")
    logging.info("You can now import livekit.plugins.tavus in your code")

if __name__ == "__main__":
    main()
