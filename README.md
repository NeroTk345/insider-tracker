# Insider Tracker

Volgt wat **insiders** (CEO's, CFO's, directeuren) met hun eigen aandelen doen en wat **superbeleggers** (Buffett, Burry, Ackman …) elk kwartaal kopen en verkopen. Alles komt rechtstreeks en gratis van de SEC (EDGAR). Bij opvallende aankopen krijg je een melding op je telefoon.

- **Form 4**: insiders moeten binnen 2 werkdagen melden wat ze kopen of verkopen.
- **13F-HR**: fondsen met meer dan $100 mln melden elk kwartaal hun Amerikaanse posities (tot 45 dagen na kwartaaleinde).

Je hebt alleen Python 3.10+ nodig. Er zijn geen packages om te installeren.

## Starten

```bash
cp config.example.json config.json
# zet je naam + e-mail in "user_agent" (de SEC eist dit, anders word je geblokkeerd)
python main.py run
```

Open daarna **http://localhost:8090**. De eerste sync duurt een paar minuten, omdat hij voor de watchlist een half jaar historie ophaalt en voor de fondsen 4 kwartalen. Daarna draait hij elk uur (`sync_interval_minutes`) en haalt hij alleen nieuwe meldingen op.

| Commando | Wat het doet |
|---|---|
| `python main.py run` | dashboard + automatische sync (aanbevolen) |
| `python main.py sync` | eenmalig alles ophalen (handig voor cron) |
| `python main.py sync --no-market` | alleen watchlist en fondsen, geen marktbrede scan |
| `python main.py serve` | alleen het dashboard |
| `python main.py test-alert` | test of je meldingen aankomen |
| `python main.py export` | statische site maken in `_site/` (voor GitHub Pages) |

## Wat zit erin

**Dashboard**
- **Overzicht**: de grootste insider-aankopen, cluster-aankopen, grote verkopen op je watchlist en de laatste alerts.
- **Insiders**: alle transacties, te filteren op type, periode, rol (CEO/officer/director), minimale waarde en ticker. Je kunt ze per transactie zien of per melding opgeteld.
- **Superbeleggers**: per fonds de portefeuille met status *nieuw / bijgekocht / afgebouwd / verkocht*, het gewicht en de wijziging t.o.v. het vorige kwartaal. Je kunt eerdere kwartalen kiezen.
- **Aandeelpagina** (via de zoekbalk): insider-aankopen en -verkopen van de afgelopen 12 maanden, welke gevolgde fondsen het aandeel hebben, en alle transacties.
- **Instellingen**: watchlist en fondsen beheren, een sync starten en een test-alert sturen.

**Alerts** (drempels in `config.json` → `alerts`)
| Regel | Standaard |
|---|---|
| Grote insider-aankoop, waar dan ook | ≥ $250k (`min_purchase_value`) |
| Elke aankoop op je watchlist | aan (`watchlist_any_purchase`) |
| Grote verkoop op je watchlist | ≥ $5 mln (`min_watchlist_sale_value`, 0 = uit) |
| Cluster-aankoop: meerdere insiders van hetzelfde bedrijf | 3 insiders binnen 14 dagen |
| Fonds dient nieuw 13F in | nieuwe posities en bijkopen ≥ 50% (met gewicht ≥ 0,5%), en volledig verkochte posities |

Alerts gaan alleen over meldingen van de laatste 3 dagen (`max_age_days`). Zo word je bij de eerste sync niet bedolven onder oude historie.

## Meldingen op je telefoon

**ntfy (het makkelijkst):** installeer de ntfy-app, abonneer je op een unieke topicnaam (bv. `nero-insiders-x7k2`) en zet die naam in `notify.ntfy_topic`. Je hebt geen account nodig. Iedereen die de topicnaam kent kan meelezen, dus kies iets dat niet te raden is.

**Telegram:** maak een bot via @BotFather, zet de token in `telegram_bot_token`. Stuur je bot een bericht en haal je chat-id op via `https://api.telegram.org/bot<TOKEN>/getUpdates`. Zet die in `telegram_chat_id`.

Test het daarna met `python main.py test-alert`.

## Fondsen toevoegen

Zoek het fonds op [sec.gov/edgar/search](https://www.sec.gov/edgar/search/) en neem de CIK over. Voeg het toe via Instellingen of in `config.json` (de `funds` in config worden alleen bij de allereerste start ingelezen). Na de sync staat de officiële SEC-naam erbij, zodat je kunt controleren of de CIK klopt.

## Datakwaliteit: wat de tracker zelf controleert

| Probleem | Hoe het is opgelost |
|---|---|
| **Gecorrigeerde insider-meldingen (Form 4/A)** | Een correctie vervangt de bijbehorende regels van de originele melding, in welke volgorde ze ook binnenkomen. Ze krijgen een grijze badge *correctie*. |
| **Gecorrigeerde 13F's (13F-HR/A)** | Een *restatement* vervangt het hele kwartaal, *new holdings* (bv. eerst geheim gehouden posities) worden toegevoegd. Het dashboard laat zien welke correcties zijn meegenomen. |
| **Tikfouten in prijzen** | Een prijs die meer dan 4× afwijkt van andere trades in hetzelfde aandeel (±45 dagen), en onlogische aantallen, krijgen een rode badge *⚠ controleer*. Alerts melden dat er dan ook bij. |
| **13F in duizenden i.p.v. dollars** | Sinds 2023 moeten fondsen in dollars rapporteren, maar sommige doen dat nog in duizenden. Dat wordt herkend (onwaarschijnlijk kleine portefeuille, of een prijs per aandeel die ~1000× afwijkt van andere fondsen) en automatisch gecorrigeerd, met een melding op de fondspagina. |
| **Tickers bij 13F-posities** | Koppeling via de CUSIP-code met [OpenFIGI](https://www.openfigi.com/api) (gratis, van Bloomberg). Alleen als dat niets oplevert, valt hij terug op de bedrijfsnaam. Zonder API-key koppelt hij ~240 codes per minuut, met een gratis key 10× zoveel. Zet de key in `openfigi.api_key`. |
| **Gemiste meldingen bij drukte** | Meldingen die niet in één run passen, gaan in een wachtrij voor de volgende run. Daarnaast worden de dagindexen van de laatste 2 werkdagen steeds opnieuw vergeleken, dus wat een eerdere run miste wordt alsnog opgehaald. |
| **Netwerkfouten** | Een melding die door een netwerkfout niet binnenkwam, wordt de volgende keer opnieuw geprobeerd. Waarschuwingen staan onder Instellingen. |

Wat níet kan worden opgelost, omdat het in de bron zit: 13F loopt tot 45 dagen achter en bevat geen shorts of buitenlandse aandelen, en Form 4-prijzen zijn vaak gemiddelden. Live koersen zitten er niet in.

## Op GitHub Pages zetten

Hier draait geen server. Een GitHub Action doet elk uur de sync, stuurt de alerts en publiceert het dashboard als statische site. De database wordt tussen runs bewaard in de Actions-cache.

1. Zet deze map in een GitHub-repo (bv. `NeroTk345/insider-tracker`) en push naar `main`.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   - `TRACKER_USER_AGENT` (verplicht), bv. `Jan Jansen jan@mail.nl`
   - optioneel: `NTFY_TOPIC`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `OPENFIGI_API_KEY`
3. **Settings → Pages → Source: GitHub Actions**.
4. **Actions → Insider Tracker (GitHub Pages) → Run workflow**. De eerste run duurt ±10 minuten, daarna staat de site op `https://<gebruiker>.github.io/<repo>/`.

Watchlist, fondsen en drempels pas je aan in **`config.pages.json`**. Na een commit draait de Action vanzelf. Zet daar geen geheimen in: die horen in de Secrets.

Goed om te weten:
- Een Pages-site is **altijd openbaar**, ook vanuit een private repo. De data is publieke SEC-data, maar je watchlist is zichtbaar.
- De site toont alle aankopen (12 maanden), verkopen vanaf $100k (90 dagen) en alles van je watchlist (2 jaar). Dit is in te stellen bij `export`.
- GitHub zet geplande Actions uit in een publieke repo zonder activiteit gedurende 60 dagen. Je krijgt dan een mail, en één klik zet ze weer aan.
- Een eigen domein (bv. `insiders.stackvault.nl`) kan via Settings → Pages → Custom domain plus een CNAME-record bij je DNS.

## Thuis hosten (Docker)

```bash
docker build -t insider-tracker .
docker run -d --name insider-tracker -p 8090:8090 \
  -v $(pwd)/config.json:/app/config.json -v $(pwd)/data:/app/data \
  --restart unless-stopped insider-tracker
```

Zet je het open op internet (bijvoorbeeld via een subdomein met een reverse proxy)? Stel dan `dashboard_password` in, en gebruik HTTPS voor de proxy.

## Goed om te weten

- **Aankopen (code P) zijn het interessantste signaal.** Een insider legt dan eigen geld in. Verkopen gebeuren vaak om andere redenen, zoals belasting of spreiding, en vaak via een vooraf ingesteld **10b5-1-plan** (die krijgen een gele badge). Toekenningen (A) en optie-uitoefeningen (M) zijn salaris, geen mening.
- **Alleen Amerikaanse filers.** ASML, Shell enzovoort vullen geen Form 4 in.
- **13F loopt achter** (tot 45 dagen) en toont alleen US long-posities en opties, geen shorts.
- **SEC-limiet:** max. 10 verzoeken per seconde. De client houdt zich daaraan. De marktbrede scan doet max. 3000 meldingen per run (`market_scan.max_filings`), de rest gaat naar de wachtrij.
- Dit is een informatietool, geen beleggingsadvies.

## Projectstructuur

```
main.py              start/CLI
tracker/sec.py       SEC-client + parsers (Form 4, 13F, dagindex, live feed)
tracker/sync.py      ophalen en opslaan
tracker/queries.py   analyses (clusters, fondswijzigingen, aandeelpagina)
tracker/alerts.py    alertregels + Telegram/ntfy
tracker/figi.py      CUSIP → ticker via OpenFIGI
tracker/export.py    statische export voor GitHub Pages
.github/workflows/   GitHub Action voor Pages
tracker/server.py    JSON-API + dashboard
web/index.html       dashboard (vanilla JS)
tests/               tests met nep-SEC-data:  python tests/test_tracker.py
```
