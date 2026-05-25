# Pre-Demo Checklist

Print this. Tick every box. T-minus 30 minutes from stage time.

## Hardware
- [ ] Laptop charged to 100 percent
- [ ] Charger in bag
- [ ] HDMI / USB-C dongle in bag (test it on the venue projector)
- [ ] Presenter remote, batteries fresh
- [ ] Phone hotspot enabled, password ready, data plan confirmed
- [ ] Backup USB stick with the recorded demo MP4

## Terminal setup
- [ ] Font size 18pt minimum (24pt preferred for back row)
- [ ] Dark theme, high-contrast palette
- [ ] Two terminals open side by side, equal width
- [ ] Both terminals at working dir `toolforge/demo/scaffold` (the tracked Vite scaffold)
- [ ] Both windows resized so the back row of the audience can read every line
- [ ] Scrollback cleared in both terminals (`clear` then resize check)
- [ ] No personal notifications, no Slack popups, no Discord, do not disturb ON

## Plugin state
- [ ] LEFT terminal: toolforge plugin NOT installed (verify `claude plugin list` shows no toolforge entry)
- [ ] RIGHT terminal: toolforge plugin installed but NO UI MCPs yet (verify `claude mcp list` shows no shadcn or magic entries, and `claude plugin list` shows `toolforge` present)
- [ ] `~/.claude/toolforge.db` deleted, OR reset to known-empty state via `demo_reset.sh -Force`
- [ ] `toolforge/demo/scaffold/reset.sh -y` run against the demo repo (PricingCard.jsx is the empty stub)
- [ ] `fallback/ui.json` and `manifest.sha256` both present, sha matches

## Network
- [ ] Wifi on, connected to venue SSID
- [ ] Captive portal already accepted in a real browser (do not let it pop up mid-demo)
- [ ] Phone hotspot tested as a backup, laptop already paired
- [ ] Browser open to a known-good test page, confirm page loads under 2 seconds
- [ ] DNS resolves (`nslookup anthropic.com` returns an IP)

## Browser tabs
- [ ] Tab 1: a screenshot or screen recording of a successful prior run
- [ ] Tab 2: OBS Studio, recording armed and started 60 seconds before walking on
- [ ] Backup video file path noted, playback verified on this machine
- [ ] Slide deck open in presenter mode on the secondary display
- [ ] No tabs with personal email, calendar, or social media

## Speaker
- [ ] Water bottle at podium, cap loosened
- [ ] Mints in pocket
- [ ] Opening line memorized word for word
- [ ] Fallback line for network failure memorized: "ToolForge is failing over to the offline cache, you will see five tools in a moment, this is the path you would get on a plane."
- [ ] Mic battery checked with venue AV crew
- [ ] Watch or stage clock visible, knows the hard stop time
- [ ] Phone on silent, face down, OUT of the podium
