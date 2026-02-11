# Hashtag United MatchDay Discord Bot

Automatisk bot der henter kampdata fra FotMob for **Hashtag United** og poster opdateringer i en Discord kanal via webhook.

## Funktioner

- Finder relevante kampe for hold-id `1186081` (Hashtag United)
- Poster notifikationer for:
  - kommende kamp (pre-match)
  - kampstart (live)
  - halftime
  - slutresultat (fulltime)
- Undgår duplikat-beskeder via lokal state-fil
- Kan køres lokalt eller via GitHub Actions på et schedule

## Opsætning

1. Opret en Discord webhook i den kanal, hvor opdateringer skal postes.
2. Sæt miljøvariabler:

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export TEAM_ID="1186081"  # optional
export PREMATCH_WINDOW_MINUTES="120"  # optional
```

3. Installer afhængigheder:

```bash
pip install -r requirements.txt
```

4. Kør scriptet:

```bash
python bot/matchday_bot.py
```

## GitHub Actions (automatisk drift)

Workflowet i `.github/workflows/fotmob-discord.yml` kører hvert 10. minut.

Tilføj repository secret:

- `DISCORD_WEBHOOK_URL`

Valgfri repository variables:

- `TEAM_ID`
- `PREMATCH_WINDOW_MINUTES`

## Bemærkninger

FotMob har ingen officiel public API til dette setup. Scriptet bruger deres åbne JSON-endpoint, som kan ændre sig over tid.
