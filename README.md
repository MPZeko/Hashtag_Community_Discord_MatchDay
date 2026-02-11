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
- Discord-beskeder sendes på engelsk

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

## Sådan tester du at botten virker

### 1) Kør automatiske tests lokalt

```bash
python -m unittest discover -s tests -v
python -m py_compile bot/matchday_bot.py tests/test_matchday_bot.py
```

Hvis begge kommandoer lykkes, er den grundlæggende event-logik og syntaks OK.

### 2) Lav en hurtig funktionel test mod Discord

1. Opret en test-kanal i Discord og lav en webhook til kanalen.
2. Sæt webhook-variablen:

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

3. Kør botten:

```bash
python bot/matchday_bot.py
```

4. Tjek at der kommer en besked i test-kanalen (hvis der findes en relevant kamp i tidsvinduet).
5. Kør kommandoen igen med det samme. Der bør normalt ikke komme dublet-beskeder, fordi botten gemmer allerede postede events i `.state/posted_events.json`.

### 3) Test via GitHub Actions (inkl. direkte post i Discord)

Når du starter workflowet manuelt (**Run workflow**), kan du vælge to testmåder:

**A. Sikker test (ingen post):**
- `dry_run = true`

**B. Direkte test-post i Discord:**
- `dry_run = false`
- `send_test_message = true`
- valgfrit: tilpas `test_message`

Med `send_test_message = true` sender workflowet én direkte besked til webhooken, så du kan verificere med det samme, at GitHub Actions kan poste i kanalen.

Tip: Discord webhook svarer ofte med HTTP `204 No Content` ved succes. Botten håndterer dette automatisk.

## GitHub Actions (automatisk drift)

Workflowet i `.github/workflows/fotmob-discord.yml` kører hvert 10. minut.

Tilføj repository secret:

- `DISCORD_WEBHOOK_URL`

Valgfri repository variables:

- `TEAM_ID`
- `PREMATCH_WINDOW_MINUTES`

Manuel `workflow_dispatch` understøtter inputs: `team_id`, `prematch_window_minutes`, `dry_run`, `send_test_message`, `test_message`.

## Bemærkninger

FotMob har ingen officiel public API til dette setup. Scriptet bruger deres åbne JSON-endpoint, som kan ændre sig over tid.
