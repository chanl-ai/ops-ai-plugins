"""The website plugin: a knowledge source over a site's pages (plugin.yaml, tools in crawl.py)."""

from ops_plugin_sdk import Plugin

from . import crawl as crawl

plugin = Plugin.beside(__file__)
