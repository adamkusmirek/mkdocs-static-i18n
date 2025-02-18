import logging
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from mkdocs import __version__ as mkdocs_version
from mkdocs import plugins
from mkdocs.commands.build import _build_page, _populate_page
from mkdocs.config.config_options import Choice, Type
from mkdocs.plugins import BasePlugin

import mkdocs_static_i18n.folder_structure as folder_structure
import mkdocs_static_i18n.suffix_structure as suffix_structure
from mkdocs_static_i18n import __file__ as installation_path
from mkdocs_static_i18n import utils
from mkdocs_static_i18n.structure import Locale

try:
    from mkdocs.localization import install_translations
except ImportError:
    install_translations = None

try:
    import pkg_resources

    material_dist = pkg_resources.get_distribution("mkdocs-material")
    material_version = material_dist.version
    material_languages = [
        lang.split(".html")[0]
        for lang in material_dist.resource_listdir("material/partials/languages")
    ]
except Exception:
    material_languages = []
    material_version = None

log = logging.getLogger("mkdocs.plugins." + __name__)

LUNR_LANGUAGES = [
    "ar",
    "da",
    "de",
    "en",
    "es",
    "fi",
    "fr",
    "hu",
    "it",
    "ja",
    "nl",
    "no",
    "pt",
    "ro",
    "ru",
    "sv",
    "th",
    "tr",
    "vi",
]
MKDOCS_THEMES = ["mkdocs", "readthedocs"]


class I18n(BasePlugin):
    """
    We use 'event_priority' to make sure that we are last plugin to be executed
    because we need to make sure that we react to other plugins' behavior
    properly.

    Current plugins we heard of and require that we control their order:
        - awesome-pages: this plugin should run before us
        - with-pdf: this plugin is triggerd by us on the appropriate on_* events
    """

    config_scheme = (
        ("default_language_only", Type(bool, default=False, required=False)),
        ("default_language", Locale(str, required=True)),
        (
            "docs_structure",
            Choice(["folder", "suffix"], default="suffix", required=False),
        ),
        ("languages", Locale(dict, required=True)),
        ("material_alternate", Type(bool, default=True, required=False)),
        ("nav_translations", Type(dict, default={}, required=False)),
        ("search_reconfigure", Type(bool, default=True, required=False)),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.i18n_configs = {}
        self.i18n_files = defaultdict(list)
        self.i18n_navs = {}
        self.material_alternates = None

    @staticmethod
    def _is_url(value):
        return value.startswith("http://") or value.startswith("https://")

    def _dict_replace_value(self, directory, old, new):
        """
        Return a copy of the given dict with value replaced.
        """
        x = {}
        for k, v in directory.items():
            if isinstance(v, dict):
                v = self._dict_replace_value(v, old, new)
            elif isinstance(v, list):
                v = self._list_replace_value(v, old, new)
            elif isinstance(v, str) or isinstance(v, Path):
                if str(v) == str(old):
                    v = new
                if not self._is_url(v):
                    v = str(Path(v))
            x[k] = v
        return x

    def _list_replace_value(self, listing, old, new):
        """
        Return a copy of the given list with value replaced.
        """
        x = []
        for e in listing:
            if isinstance(e, list):
                e = self._list_replace_value(e, old, new)
            elif isinstance(e, dict):
                e = self._dict_replace_value(e, old, new)
            elif isinstance(e, str) or isinstance(e, Path):
                if str(e) == str(old):
                    e = new
                if not self._is_url(e):
                    e = str(Path(e))
            x.append(e)
        return x

    def _set_languages_options(self, config):
        """
        Configure languages options for the 'default' language
        """
        # Set the 'site_name' for all configured languages
        for language, lang_config in self.config["languages"].items():
            localized_site_name = lang_config["site_name"] or config["site_name"]
            self.config["languages"][language]["site_name"] = localized_site_name
        # the default_language options can be made available for the
        # 'default' / version of the website
        self.default_language_options = self.config["languages"].pop(
            "default",
            {
                "name": "default",
                "link": "./",
                "fixed_link": None,
                "build": True,
                "site_name": config["site_name"],
            },
        )
        if self.default_language_options["name"] == "default":
            default_language_config = self.config["languages"].get(
                self.default_language, {}
            )
            self.default_language_options["name"] = default_language_config.get(
                "name", self.default_language
            )
            self.default_language_options["site_name"] = default_language_config.get(
                "site_name", config["site_name"]
            )
            self.default_language_options["fixed_link"] = default_language_config.get(
                "fixed_link", None
            )
        # when the default language is listed on the languages
        # this means that the user wants a /default_language version
        # of his website
        if self.default_language not in self.config["languages"]:
            # no other language than default language set?
            if len(self.config["languages"]) == 0:
                build = True
            else:
                build = False
            self.config["languages"][self.default_language] = {
                "name": self.default_language_options["name"],
                "link": "./",
                "fixed_link": None,
                "build": build,
                "site_name": config["site_name"],
            }

    @plugins.event_priority(-100)
    def on_config(self, config, **kwargs):
        """
        Enrich configuration with language specific knowledge.
        """
        self.default_language = self.config["default_language"]
        self._set_languages_options(config)
        # Make an order preserving list of all the configured languages
        self.all_languages = []
        for language in self.config["languages"]:
            if language not in self.all_languages:
                self.all_languages.append(language)
        if self.default_language not in self.all_languages:
            self.all_languages.insert(0, self.default_language)
        # Make sure with-pdf is controlled by us, see #110
        # We remove all events and will trigger them ourselves
        # TODO: can we totally get rid of this?
        if "with-pdf" in config["plugins"]:
            for events in config["plugins"].events.values():
                for idx, event in enumerate(list(events)):
                    try:
                        if str(event.__module__) == "mkdocs_with_pdf.plugin":
                            events.pop(idx)
                    except AttributeError:
                        # partials don't have a module
                        pass
        # Make a localized copy of the config
        # The hooks are mutualized so we remove them from the config before (deep)copying
        # The plugins are mutualized so we remove them from the config before (deep)copying
        if "hooks" in config:
            hooks = config.pop("hooks")
        else:
            hooks = None
        plugins = config.pop("plugins")
        for language in self.all_languages:
            self.i18n_configs[language] = deepcopy(config)
            self.i18n_configs[language]["plugins"] = plugins
            if hooks:
                self.i18n_configs[language]["hooks"] = hooks
        config["plugins"] = plugins
        if hooks:
            config["hooks"] = hooks
        # Set theme locale to default language
        if self.default_language != "en":
            if config["theme"].name in MKDOCS_THEMES:
                if mkdocs_version >= "1.2":
                    config["theme"]["locale"] = self.default_language
                    log.info(
                        f"Setting the default 'theme.locale' option to '{self.default_language}'"
                    )
            elif config["theme"].name == "material":
                config["theme"].language = self.default_language
                log.info(
                    f"Setting the default 'theme.language' option to '{self.default_language}'"
                )
        # Skip language builds requested?
        if self.config["default_language_only"] is True:
            return config
        # Support for mkdocs-material>=7.1.0 language selector
        if self.config["material_alternate"] and len(self.all_languages) > 1:
            if material_version and material_version >= "7.1.0":
                if not config["extra"].get("alternate") or kwargs.get("force"):
                    config["extra"]["alternate"] = []
                    # Add index.html file name when used with
                    # use_directory_urls = True
                    link_suffix = ""
                    if config.get("use_directory_urls") is False:
                        link_suffix = "index.html"
                    # TODO: document
                    if self.default_language_options["build"] is True:
                        config["extra"]["alternate"].append(
                            {
                                "name": f"{self.default_language_options['name']}",
                                "link": f"{self.default_language_options['link']}{link_suffix}",
                                "fixed_link": self.default_language_options[
                                    "fixed_link"
                                ],
                                "lang": self.default_language,
                            }
                        )
                    for language, lang_config in self.config["languages"].items():
                        if lang_config["build"] is True:
                            if (
                                self.default_language_options["build"] is True
                                and lang_config["name"]
                                == self.default_language_options["name"]
                            ):
                                continue
                            config["extra"]["alternate"].append(
                                {
                                    "name": f"{lang_config['name']}",
                                    "link": f"{lang_config['link']}{link_suffix}",
                                    "fixed_link": lang_config["fixed_link"],
                                    "lang": language,
                                }
                            )
                elif "alternate" in config["extra"]:
                    for alternate in config["extra"]["alternate"]:
                        if not alternate.get("link", "").startswith("./"):
                            log.info(
                                "The 'extra.alternate' configuration contains a "
                                "'link' option that should starts with './' in "
                                f"{alternate}"
                            )

                if "navigation.instant" in config["theme"]._vars.get("features", []):
                    log.warning(
                        "mkdocs-material language switcher contextual link is not "
                        "compatible with theme.features = navigation.instant"
                    )
                else:
                    self.material_alternates = config["extra"].get("alternate")
        # Support for the search plugin lang
        search_plugin = utils.get_plugin("search", config)
        if self.config["search_reconfigure"] and search_plugin:
            search_langs = search_plugin.config["lang"] or []
            for language in self.all_languages:
                if language in LUNR_LANGUAGES:
                    if language not in search_langs:
                        search_langs.append(language)
                        log.info(
                            f"Adding '{language}' to the 'plugins.search.lang' option"
                        )
                else:
                    log.warning(
                        f"Language '{language}' is not supported by "
                        f"lunr.js, not setting it in the 'plugins.search.lang' option"
                    )
        # Report misconfigured nav_translations, see #66
        if self.config["nav_translations"]:
            for lang in self.config["languages"]:
                if lang in self.config["nav_translations"]:
                    break
            else:
                log.info(
                    "Ignoring 'nav_translations' option: expected a language key "
                    f"from {list(self.config['languages'].keys())}, got "
                    f"{list(self.config['nav_translations'].keys())}"
                )
                self.config["nav_translations"] = {}
        # Install a i18n aware version of sitemap.xml if not provided by the user
        if not Path(
            Path(config["theme"]._vars.get("custom_dir", ".")) / Path("sitemap.xml")
        ).exists():
            custom_i18n_sitemap_dir = Path(
                Path(installation_path).parent / Path("custom_i18n_sitemap")
            ).resolve()
            config["theme"].dirs.insert(0, str(custom_i18n_sitemap_dir))
        return config

    @plugins.event_priority(-100)
    def on_files(self, files, config):
        """
        Construct the main + lang specific file tree which will be used to
        generate the navigation for the default site and per language.
        """
        if self.config["docs_structure"] == "suffix":
            return suffix_structure.on_files(self, files, config)
        else:
            return folder_structure.on_files(self, files, config)

    def _fix_config_navigation(self, language, files):
        """
        When a static navigation is set in mkdocs.yml a user will usually
        structurate its navigation using the main (default language)
        documentation markdown pages.

        This function localizes the given pages to their translated
        counterparts if available.
        """
        for i18n_page in files.documentation_pages():
            if Path(i18n_page.src_path).suffixes == [f".{language}", ".md"]:
                config_path_expects = [
                    i18n_page.non_i18n_src_path.with_suffix(".md"),
                    i18n_page.non_i18n_src_path.with_suffix(
                        f".{self.default_language}.md"
                    ),
                ]
                for config_path in config_path_expects:
                    self.i18n_configs[language]["nav"] = self._list_replace_value(
                        self.i18n_configs[language]["nav"],
                        config_path,
                        i18n_page.src_path,
                    )

    def _maybe_translate_titles(self, language, items):
        translated = False
        translated_nav = self.config["nav_translations"].get(language, {})
        if translated_nav:
            for item in items:
                if hasattr(item, "title") and item.title in translated_nav:
                    log.debug(
                        f"Translating {type(item).__name__} title '{item.title}' "
                        f"({self.default_language}) to "
                        f"'{translated_nav[item.title]}' ({language})"
                    )
                    item.title = translated_nav[item.title]
                    translated = True
                if hasattr(item, "children") and item.children:
                    translated = (
                        self._maybe_translate_titles(language, item.children)
                        or translated
                    )
        return translated

    @plugins.event_priority(-100)
    def on_nav(self, nav, config, files):
        """
        Translate i18n aware navigation to honor the 'nav_translations' option.
        """
        if self.config["docs_structure"] == "suffix":
            nav = suffix_structure.on_nav(self, nav, config, files)
        else:
            nav = folder_structure.on_nav(self, nav, config, files)
        # Manually trigger with-pdf on_nav, see #110
        with_pdf_plugin = config["plugins"].get("with-pdf")
        if with_pdf_plugin:
            with_pdf_plugin.on_nav(nav, config, files)
        return nav

    def _fix_search_duplicates(self, search_plugin):
        """
        We want to avoid indexing the same pages twice if the default language
        has its own version built as well as the /language version too as this
        would pollute the search results.

        When this happens, we favor the default language location if its
        content is the same as its /language counterpart.
        """
        attribute_name = (
            "_entries" if hasattr(search_plugin.search_index, "_entries") else "entries"
        )
        try:
            search_index_entries = getattr(search_plugin.search_index, attribute_name)
        except AttributeError:
            log.warning(f"Can't access the search index entries in {search_plugin}.")
            return
        default_lang_entries = filter(
            lambda x: not x["location"].startswith(
                tuple(self.config["languages"].keys())
            ),
            search_index_entries,
        )
        target_lang_entries = list(
            filter(
                lambda x: x["location"].startswith(
                    tuple(self.config["languages"].keys())
                ),
                search_index_entries,
            )
        )
        for default_lang_entry in default_lang_entries:
            duplicated_entries = filter(
                lambda x: x["title"] == default_lang_entry["title"]
                and x["location"].endswith(x["location"])
                and x["text"] == default_lang_entry["text"],
                target_lang_entries,
            )
            for duplicated_entry in duplicated_entries:
                if duplicated_entry in search_index_entries:
                    log.debug(
                        f"removed duplicated search entry: {duplicated_entry['title']} "
                        f"{duplicated_entry['location']}"
                    )
                    search_index_entries.remove(duplicated_entry)

    @plugins.event_priority(-100)
    def on_env(self, env, config, files, **kwargs):
        """
        Copy the main env because it can have been altered by hooks.

        Since hooks are injected as plugins, we need to make sure to
        run last using event_priority.

        See #178.
        """
        for language in self.config["languages"].keys():
            self.i18n_configs[language]["env"] = env
        return env

    @plugins.event_priority(-100)
    def on_page_markdown(self, markdown, page, config, files):
        """
        Use the 'page_markdown' event to translate page titles as well
        because they are used in the bottom navigation links for
        previous/next links titles.

        This event is triggered right after the page.read_source() has
        been executed so we get the definitive 'page.title' that mkdocs
        discovered and we just have to translate if it needed.
        """
        page.locale = page.file.dest_language or self.default_language
        if self.config["nav_translations"].get(page.locale, {}):
            self._maybe_translate_titles(page.locale, [page])
        return markdown

    @plugins.event_priority(-100)
    def on_page_context(self, context, page, config, nav):
        """
        Make the language switcher contextual to the current page.

        This allows to switch language while staying on the same page.
        """
        # export some useful i18n related variables on page context, see #75
        context["i18n_config"] = self.config
        context["i18n_page_locale"] = page.locale
        context["i18n_page_file_locale"] = page.file.locale_suffix

        if self.material_alternates:
            alternates = deepcopy(self.material_alternates)
            page_url = page.url
            for language in self.all_languages:
                if page.url.startswith(f"{language}/"):
                    prefix_len = len(language) + 1
                    page_url = page.url[prefix_len:]
                    break

            for alternate in alternates:
                if config.get("use_directory_urls") is False:
                    alternate["link"] = alternate["link"].replace("/index.html", "", 1)
                fixed_link = alternate.get("fixed_link")
                if fixed_link:
                    alternate["link"] = fixed_link
                else:
                    if not alternate["link"].endswith("/"):
                        alternate["link"] += "/"
                    alternate["link"] += page_url
            config["extra"]["alternate"] = alternates

        # set the localized site_name if any
        if page.file.dest_language == "":
            # default
            localized_site_name = self.default_language_options["site_name"]
        else:
            localized_site_name = (
                self.config["languages"]
                .get(context["i18n_page_locale"], {})
                .get("site_name", config["site_name"])
            )
        config["site_name"] = localized_site_name

        return context

    @plugins.event_priority(-100)
    def on_post_page(self, output, page, config):
        """
        Some plugins we control ourselves need this event.
        """
        # Manually trigger with-pdf on_nav, see #110
        with_pdf_plugin = config["plugins"].get("with-pdf")
        if with_pdf_plugin:
            with_pdf_plugin.on_post_page(output, page, config)
        return output

    @plugins.event_priority(-100)
    def on_post_build(self, config):
        """
        Derived from mkdocs commands build function.

        We build every language on its own directory.
        """
        # user requested only the default version to be built
        if self.config["default_language_only"] is True:
            return

        dirty = False
        minify_plugin = config["plugins"].get("minify")
        search_plugin = utils.get_plugin("search", config)
        with_pdf_plugin = config["plugins"].get("with-pdf")
        if with_pdf_plugin:
            with_pdf_plugin.on_post_build(config)
            with_pdf_output_path = with_pdf_plugin.config["output_path"]
        for language, language_config in self.config["languages"].items():
            # Language build disabled by the user, skip
            if language_config["build"] is False:
                log.info(f"Skipping building {language} documentation")
                continue

            log.info(f"Building {language} documentation")

            config = self.i18n_configs[language]
            env = self.i18n_configs[language]["env"]
            files = self.i18n_files[language]
            nav = self.i18n_navs[language]

            # Support mkdocs-material theme language
            if config["theme"].name == "material":
                if language in material_languages:
                    config["theme"].language = language
                else:
                    log.warning(
                        f"Language {language} is not supported by "
                        f"mkdocs-material=={material_version}, not setting "
                        "the 'theme.language' option"
                    )

            # Support mkdocs-minify-plugin
            if minify_plugin:
                minify_plugin.on_pre_build(config=config)

            # Include theme specific files
            files.add_files_from_theme(env, config)

            # Include static files
            files.copy_static_files(dirty=dirty)

            for file in files.documentation_pages():
                _populate_page(file.page, config, files, dirty)

            for file in files.documentation_pages():
                _build_page(file.page, config, files, nav, env, dirty)

            if with_pdf_plugin:
                with_pdf_plugin.config[
                    "output_path"
                ] = f"{language}/{with_pdf_output_path}"
                with_pdf_plugin.on_config(config)
                with_pdf_plugin.on_nav(nav, config, files)
                with_pdf_plugin.on_post_build(config)

        # Update the search plugin index with language pages
        if search_plugin:
            self._fix_search_duplicates(search_plugin)
            search_plugin.on_post_build(config=config)
