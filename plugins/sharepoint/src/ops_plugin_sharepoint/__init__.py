"""The SharePoint and OneDrive plugin: plugin.yaml beside this file, its tools in files.py."""

from ops_plugin_sdk import Plugin

from . import files as files

plugin = Plugin.beside(__file__)
