def post_init_hook(env):
    """Create the config row.

    The rule pack does NOT load here: it loads via data/ruleset_load.xml
    (a <function> data file), which Odoo re-executes on every install
    AND upgrade. A post_init_hook only fires on first install, leaving
    upgrades without new pack rules.
    """
    env["waf.config"].get_singleton()
