# Premier League Leaderboard Bot

This version uses **one LB command only: `/createlb`**. There is no `/sendlb`.

## Commands
- `/sendchallenge` — Owner only. Sends the main Overall/Mobile challenge panel.
- `/createlb board region` — Owner only. Creates **and sends** the 10 individual LB cards in the current channel. Running it again refreshes that LB instead of making duplicates.
- `/setannouncement ...` — Referee + Ranking Supervisor. Creates a global set ID, includes FT, and sends it to the approval channel.
- `/scoreannouncement ...` — Referee + Ranking Supervisor. Gets the LB, players, spots and FT from the set ID, records manual/auto result and updates the LB.
- `/voidset set_id` — Owner only. Removes the set and its recorded announcement/score, then shifts every later global set ID back by 1.
- `/deletechannel name` — Referee/Supervisor/Owner. Saves a transcript to the log channel and deletes the challenge channel.
- `/editprofile profile_id ...` — Editor. Updates the stored profile and Roblox avatar.
- `/deleteprofile profile_id` — Editor. Deletes the profile and clears every LB spot belonging to that profile.
- `/clearspot board region spot` — Editor. Vacates a spot and refreshes the LB.
- `/editspot board region spot stage` — Editor. Changes the **LB stage only**. Stage is not part of profile creation.

## LB display
Each `/createlb` creates 10 separate compact embeds, matching the requested style:
- `#1` through `#10`
- player nickname
- Discord mention
- Roblox username
- country flag
- stage
- Challengeable / Protection / Cooldown status
- Roblox avatar thumbnail
- vacant spots show `Vacant`

The bot automatically refreshes the stored LB cards after claims, swaps, clears, profile edits, results, and expired Protection/Cooldown statuses.

## Challenge flow
`/sendchallenge` -> Overall/Mobile -> Region -> Spot.

- Vacant spot: claim if the player meets the board role requirement.
- Ranked player: can challenge any player above them.
- Range is 1. The immediately higher spot gets the normal Fight/Auto panel. Further-above challenges use Accept/Dodge/Auto.
- Protection also gives the challenged player Dodge.
- Cooldown players cannot be challenged.
- An unranked player with a full LB can challenge only #9 or #10.
- A player moving into a vacant higher spot has their old spot cleared.
- A player cannot claim a spot below/equal to their current spot.
- Profile is requested only once, on the first claim/challenge, and then the original action continues automatically.

## Result rules
- Defender wins: defender gets 3-day Protection; challenger gets 3-day Cooldown.
- Challenger wins: challenger gets no status; defeated defender gets 1-day Cooldown.
- Auto has no score and no referee field, only the reason.
- If the challenged player chooses Auto, the challenger wins.
- If the challenger chooses Auto, the challenged player wins.
- Only the challenger and challenged player's two spots are swapped/changed.
- Challenge channels are private to the two players plus Referee, Ranking Supervisor and Owner.
- Challenge channels are deleted after 30 minutes. Auto/Dodge/manual deletion has an appropriate deletion reason in the log.
- Challenge transcripts are saved as files in the challenge log channel.

## Profiles
Profile asks only for:
- LB nickname
- Discord ID
- Roblox username
- Country flag

Roblox is verified through Roblox's public API and the avatar is automatically stored and shown on the LB. The profile channel uses the requested `Player Profile — ID 001` style and includes the fake-information warning.

## Set system
`/setannouncement` uses the player Discord IDs and spots supplied by the referee, plus time/GMT and FT. It does **not** ask for player nicknames, Roblox usernames, country or stage.

Every set receives one global ID. Approval is only by Ranking Supervisor. The approval request is deleted after approval/rejection. Approved sets are posted to the announcement channel with the set ping role.

`/scoreannouncement` gets the set information from the ID. Manual results require the score and can include a referee. Auto results require only a reason. FT is also taken from the set ID.

`/voidset` removes the original set/score messages and tells everyone that later set IDs will get behind by 1. Later visible set IDs are updated too.

## Required environment variables
See `.env.example`. `DISCORD_BOT_TOKEN` is required. `GUILD_ID` is recommended for fast slash-command syncing.
