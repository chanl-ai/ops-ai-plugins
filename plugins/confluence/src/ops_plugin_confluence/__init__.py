"""The Confluence plugin: plugin.yaml beside this file, its tools in pages.py."""

from ops_plugin_sdk import Plugin

from . import pages as pages

plugin = Plugin.beside(__file__)
