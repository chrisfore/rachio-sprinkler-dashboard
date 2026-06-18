#!/usr/bin/env python3
"""
Generate a Rachio sprinkler dashboard for Home Assistant from your own
`rachio_local` entities — no hand-editing of entity IDs.

It reads your entities from the HA REST API and writes two files:
  - sprinklers.yaml          (paste into a dashboard's Raw configuration editor)
  - rachio_dashboard.yaml    (drop into <config>/packages/)

Usage:
  HA_URL=http://homeassistant.local:8123 HA_TOKEN=xxxx python3 generate_dashboard.py
or:
  python3 generate_dashboard.py --url http://homeassistant.local:8123 --token xxxx

Requires only the Python standard library. Dashboard needs HACS cards
"Mushroom", "card-mod", and "auto-entities".
"""
import argparse, json, os, re, sys, urllib.request, urllib.error

BLUE = "linear-gradient(135deg,#1565e0,#2196f3)"

def get_states(url, token):
    req = urllib.request.Request(url.rstrip("/") + "/api/states",
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"HA API error {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        sys.exit(f"Could not reach HA at {url}: {e}")

def zone_label(friendly):
    # "Rachio Zone: Front Lawn" -> "Front Lawn"
    if "Zone:" in friendly:
        return friendly.split("Zone:", 1)[1].strip()
    return friendly.strip()

def icon_for(name):
    n = name.lower()
    if any(w in n for w in ("drip", "soaker")):
        return "mdi:water", "blue"
    if any(w in n for w in ("planter", "flower", "pot", "bed", "garden")):
        return "mdi:flower", "amber"
    if any(w in n for w in ("tree", "shrub", "bush")):
        return "mdi:tree", "green"
    return "mdi:grass", "green"  # default: lawn

def zone_card(eid, name, icon, color):
    chips = "\n".join(
        f"""                      - type: template
                        content: {mins}m
                        tap_action: {{ action: call-service, service: rachio_local.turn_on, target: {{ entity_id: {eid} }}, data: {{ duration: {mins*60} }} }}"""
        for mins in (5, 10, 15, 20)
    )
    return f"""              - type: vertical-stack
                cards:
                  - type: custom:mushroom-template-card
                    entity: {eid}
                    primary: '{{{{ (state_attr("{eid}","friendly_name") or "{name}").split("Zone:")[-1].strip() }}}}'
                    secondary: '{{{{ "Watering now" if is_state("{eid}","on") else "Tap minutes below to water" }}}}'
                    icon: '{{{{ "mdi:sprinkler-variant" if is_state("{eid}","on") else "{icon}" }}}}'
                    icon_color: '{{{{ "#ffffff" if is_state("{eid}","on") else "{color}" }}}}'
                    tap_action:
                      action: more-info
                    card_mod:
                      style: |
                        ha-card {{
                          background: none !important; box-shadow: none !important; border: none !important; margin: 0 !important;
                          {{% if is_state(config.entity, 'on') %}}
                          --primary-text-color: #ffffff;
                          --secondary-text-color: rgba(255,255,255,0.9);
                          {{% endif %}}
                        }}
                  - type: custom:mushroom-chips-card
                    alignment: center
                    chips:
{chips}
                      - type: template
                        icon: mdi:stop
                        icon_color: red
                        content: Stop
                        tap_action: {{ action: call-service, service: rachio_local.turn_off, target: {{ entity_id: {eid} }} }}
                    card_mod:
                      style: |
                        ha-card {{ background: none !important; box-shadow: none !important; border: none !important; }}
                card_mod:
                  style: |
                    #root {{
                      border: 1px solid var(--divider-color);
                      border-radius: 16px;
                      overflow: hidden;
                      padding-bottom: 6px;
                      {{% if is_state("{eid}","on") %}}
                      background: {BLUE};
                      border-color: #5cb0ff;
                      box-shadow: 0 0 16px rgba(33,150,243,0.45);
                      {{% else %}}
                      background: var(--card-background-color);
                      {{% endif %}}
                    }}"""

def slugify(s):
    # Close enough to HA's entity_id slugging to build a glob prefix.
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", s.lower())).strip("_")

def programs_block(sched_glob, dev_prefix):
    # One auto-entities card that auto-discovers every Rachio program
    # (switch.<controller>_*_schedule), hides unavailable/orphaned ones, and
    # renders a tappable card each whose label reads the live entity name —
    # so programs added / removed / renamed in the Rachio app appear here
    # automatically after a rachio_local reload. Needs the HACS "auto-entities".
    strip_dev = f' | replace("{dev_prefix} ","")' if dev_prefix else ""
    return f"""          - type: custom:auto-entities
            card:
              type: grid
              columns: 1
              square: false
            card_param: cards
            sort:
              method: friendly_name
            filter:
              include:
                - entity_id: {sched_glob}
                  options:
                    type: custom:mushroom-template-card
                    entity: this.entity_id
                    primary: '{{{{ (state_attr(config.entity,"friendly_name") or ""){strip_dev} | replace(" Schedule","") | trim }}}}'
                    secondary: '{{{{ "Running now" if is_state(config.entity,"on") else "Tap to start · hold to stop" }}}}'
                    icon: mdi:calendar-check
                    icon_color: '{{{{ "blue" if is_state(config.entity,"on") else "green" }}}}'
                    tap_action:
                      action: call-service
                      service: switch.turn_on
                      target:
                        entity_id: this.entity_id
                    hold_action:
                      action: call-service
                      service: switch.turn_off
                      target:
                        entity_id: this.entity_id
              exclude:
                - state: unavailable
                - state: unknown"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("HA_URL"))
    ap.add_argument("--token", default=os.environ.get("HA_TOKEN"))
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    if not a.url or not a.token:
        sys.exit("Set HA_URL and HA_TOKEN (env or --url/--token).")

    states = get_states(a.url, a.token)
    by_id = {e["entity_id"]: e for e in states}

    zones, scheds = [], []
    ctrl_status = rain = None
    last_watered = []
    for e in states:
        eid = e["entity_id"]; fn = e["attributes"].get("friendly_name", eid)
        if eid.startswith("switch.") and eid.endswith("_zone"):
            zones.append(e)
        elif eid.startswith("switch.") and eid.endswith("_schedule"):
            scheds.append(e)
        elif eid.startswith("sensor.") and eid.endswith("_status") and e["state"] in ("online", "offline"):
            ctrl_status = eid
        elif eid.startswith("sensor.") and eid.endswith("_rain_sensor_tripped"):
            rain = eid
        elif eid.startswith("sensor.") and eid.endswith("_last_watered"):
            last_watered.append(e)
    if not zones:
        sys.exit("No rachio_local zone switches (switch.*_zone) found. Is the integration loaded?")
    zones.sort(key=lambda e: zone_label(e["attributes"].get("friendly_name", "")))
    scheds.sort(key=lambda e: e["attributes"].get("friendly_name", ""))
    last_watered.sort(key=lambda e: e["attributes"].get("friendly_name", ""))

    # Controller name prefix (e.g. "Rachio-Gen3"), derived from a zone's friendly_name
    # ("<controller> Zone: <name>"). Used to strip it from schedule labels at load time.
    z0 = zones[0]["attributes"].get("friendly_name", "")
    dev_prefix = z0.split(" Zone:")[0].strip() if " Zone:" in z0 else ""

    # --- header chips (only include what exists); indented to sit under `chips:` ---
    chips = []
    if ctrl_status:
        chips.append(f"""                  - type: template
                    entity: {ctrl_status}
                    icon: mdi:access-point
                    icon_color: '{{{{ "green" if is_state("{ctrl_status}","online") else "red" }}}}'
                    content: '{{{{ "Online" if is_state("{ctrl_status}","online") else "Offline" }}}}'
                    tap_action: {{ action: more-info }}""")
    if rain:
        chips.append(f"""                  - type: template
                    entity: {rain}
                    icon: mdi:weather-pouring
                    icon_color: '{{{{ "blue" if is_state("{rain}","True") else "disabled" }}}}'
                    content: '{{{{ "Rain detected" if is_state("{rain}","True") else "No rain" }}}}'
                    tap_action: {{ action: more-info }}""")
    refresh_target = ctrl_status or zones[0]["entity_id"]
    chips.append(f"""                  - type: template
                    icon: mdi:refresh
                    content: Refresh
                    tap_action: {{ action: call-service, service: homeassistant.update_entity, target: {{ entity_id: [{refresh_target}] }} }}""")
    chips_yaml = "\n".join(chips)

    # Globs for the dynamic sections, derived from the controller slug
    # (e.g. "Rachio-Gen3" -> "rachio_gen3"). Fall back to a broad match.
    slug = slugify(dev_prefix) if dev_prefix else ""
    sched_glob = f"switch.{slug}_*_schedule" if slug else "switch.*_schedule"
    sw_prefix = f"switch.{slug}_" if slug else "switch."

    zone_yaml = "\n".join(zone_card(e["entity_id"],
                                    zone_label(e["attributes"].get("friendly_name", e["entity_id"])),
                                    *icon_for(zone_label(e["attributes"].get("friendly_name", ""))))
                          for e in zones)

    # Watering programs: a single auto-entities card that auto-discovers every
    # Rachio schedule at load time (no per-program hand-listing).
    programs_yaml = programs_block(sched_glob, dev_prefix)

    # Recent activity: one row per zone, using the zone's clean label + its
    # matching *_last_watered sensor (derived from the zone switch entity_id).
    recent_rows = []
    for e in zones:
        zeid = e["entity_id"]
        lw = zeid.replace("switch.", "sensor.")
        lw = (lw[:-5] if lw.endswith("_zone") else lw) + "_last_watered"
        if lw in by_id:
            recent_rows.append(f"""              - entity: {lw}""")
    recent_yaml = "\n".join(recent_rows) or f"              - entity: {zones[0]['entity_id']}"

    stop_target = zones[0]["entity_id"]

    dashboard = f"""# Rachio Sprinkler Dashboard — generated for your entities.
# Requires HACS cards: Mushroom + card-mod + auto-entities, and packages/rachio_dashboard.yaml
title: Sprinklers
views:
  - title: Sprinklers
    path: sprinklers
    icon: mdi:sprinkler-variant
    cards:
      - type: vertical-stack
        cards:
          - type: custom:mushroom-title-card
            title: Sprinklers
            subtitle: At a glance
          - type: vertical-stack
            cards:
              - type: custom:mushroom-chips-card
                alignment: center
                chips:
{chips_yaml}
                card_mod:
                  style: |
                    ha-card {{ background: none !important; box-shadow: none !important; border: none !important; }}
            card_mod:
              style: |
                #root {{
                  background: var(--card-background-color);
                  border: 1px solid var(--divider-color);
                  border-radius: 16px;
                  padding: 8px 4px;
                }}
          - type: custom:mushroom-template-card
            primary: Stop all watering
            icon: mdi:stop-circle
            icon_color: white
            layout: horizontal
            multiline_secondary: false
            tap_action:
              action: call-service
              service: script.rachio_stop_all
              confirmation:
                text: Stop all watering right now?
            card_mod:
              style: |
                {{% set ns = namespace(on=false) %}}
                {{% for s in states.switch %}}
                {{% if s.entity_id.startswith('{sw_prefix}') and (s.entity_id.endswith('_zone') or s.entity_id.endswith('_schedule')) and s.state == 'on' %}}{{% set ns.on = true %}}{{% endif %}}
                {{% endfor %}}
                :host {{ {{% if not ns.on %}}display: none !important;{{% endif %}} }}
                ha-card {{
                  background: #e53935; border: none; border-radius: 16px;
                  --primary-text-color: #ffffff; --card-primary-color: #ffffff; --icon-primary-color: #ffffff;
                  font-weight: 700;
                }}

      - type: vertical-stack
        cards:
          - type: custom:mushroom-title-card
            subtitle: Zones
          - type: grid
            columns: 1
            square: false
            cards:
{zone_yaml}

      - type: vertical-stack
        cards:
          - type: custom:mushroom-title-card
            subtitle: Watering programs
{programs_yaml}

      - type: vertical-stack
        cards:
          - type: custom:mushroom-title-card
            subtitle: Recent activity
          - type: entities
            entities:
{recent_yaml}
"""

    package = f"""# Rachio dashboard support package — only a "stop all watering" script.
# Place at <config>/packages/rachio_dashboard.yaml and ensure configuration.yaml has:
#   homeassistant:
#     packages: !include_dir_named packages
script:
  rachio_stop_all:
    alias: Rachio stop all watering
    mode: single
    sequence:
      # Rachio has no per-zone stop: turning off ANY zone stops the whole controller.
      - service: switch.turn_off
        target:
          entity_id: {stop_target}
"""

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "sprinklers.yaml"), "w") as f:
        f.write(dashboard)
    with open(os.path.join(a.out, "rachio_dashboard.yaml"), "w") as f:
        f.write(package)
    print(f"Wrote sprinklers.yaml ({len(zones)} zones, {len(scheds)} schedules) and rachio_dashboard.yaml to {a.out}/")

if __name__ == "__main__":
    main()
