"""Sphinx configuration."""

from sphinx.application import Sphinx

project = "Fluvo"
author = "bosd"
copyright = "2025, bosd"
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinxmermaid",
    "sphinx_click",
    "myst_parser",
    "sphinx_copybutton",
]
autodoc_typehints = "description"
html_theme = "shibuya"

# The name of an image file (relative to this directory) to place at the top
# of the sidebar.
#
html_logo = "_static/icon.png"


# The name of an image file (relative to this directory) to use as a favicon of
# the docs.  This file should be a Windows icon file (.ico) being 16x16 or 32x32
# pixels large.
html_favicon = "_static/favicon.ico"
html_static_path = ["_static"]
# Fluvo brand palette / logo sizing.
html_css_files = ["branding.css"]

# Link the docs back to the GitHub repository: a GitHub icon in the header
# (github_url) plus the shibuya "Edit this page" / repo-stats sidebars, which
# read the source location from html_context.
html_theme_options = {
    "github_url": "https://github.com/GetFluvo/fluvo",
}
html_context = {
    "source_type": "github",
    "source_user": "GetFluvo",
    "source_repo": "fluvo",
    "source_version": "master",
    "source_docs_path": "/docs/",
}

# -- Link checking (sphinx linkcheck builder) -------------------------------
# The scheduled link check hits live external URLs that intermittently time out or
# rate-limit (e.g. www.contributor-covenant.org). Retry transient failures and do
# not treat a timeout as broken, so the run only fails on a genuinely dead link
# (a hard 404/error), not a one-off network blip.
linkcheck_retries = 3
linkcheck_timeout = 30
linkcheck_report_timeouts_as_broken = False


def on_builder_inited(app: Sphinx) -> None:
    """This function is connected to the 'builder-inited' event.

    It removes the sphinx-mermaid extension if the builder is LaTeX, as it is
    not compatible with PDF output.
    """
    if app.builder.name == "latex":
        if "sphinx_mermaid" in extensions:
            extensions.remove("sphinx_mermaid")


# -- Setup function for builder-specific configuration ----------------------
def setup(app: Sphinx) -> None:
    """Called by Sphinx during the build process.

    We use this to disable extensions that are not compatible with certain
    builders, like LaTeX/PDF.
    """
    # The sphinx-mermaid extension is not compatible with the LaTeX builder,
    # so we remove it from the extensions list only when building for PDF.
    app.connect("builder-inited", on_builder_inited)
