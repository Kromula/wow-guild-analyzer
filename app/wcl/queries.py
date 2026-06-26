"""GraphQL query strings for the WarcraftLogs v2 API."""

# Paginated list of a guild's reports within an optional time window.
GUILD_REPORTS = """
query GuildReports($guildName: String!, $serverSlug: String!, $serverRegion: String!,
                   $startTime: Float, $endTime: Float, $page: Int!, $limit: Int!) {
  reportData {
    reports(guildName: $guildName, guildServerSlug: $serverSlug, guildServerRegion: $serverRegion,
            startTime: $startTime, endTime: $endTime, page: $page, limit: $limit) {
      total
      current_page
      has_more_pages
      data {
        code
        title
        startTime
        endTime
        zone { id name }
      }
    }
  }
}
"""

# Fights + player roster for a single report.
REPORT_FIGHTS = """
query ReportFights($code: String!) {
  reportData {
    report(code: $code) {
      title
      startTime
      endTime
      fights(killType: Encounters) {
        id
        name
        encounterID
        difficulty
        kill
        startTime
        endTime
        fightPercentage
        friendlyPlayers
      }
      masterData {
        actors(type: "Player") {
          id
          name
          subType
          server
        }
      }
    }
  }
}
"""

# A single analysis table (Damage/Healing/Deaths/Casts/...) for a time window in a report.
# `table` returns provider-shaped JSON which we parse in the normalizer.
REPORT_TABLE = """
query ReportTable($code: String!, $startTime: Float!, $endTime: Float!,
                  $dataType: TableDataType!, $fightIDs: [Int]) {
  reportData {
    report(code: $code) {
      table(dataType: $dataType, startTime: $startTime, endTime: $endTime, fightIDs: $fightIDs)
    }
  }
}
"""

# Raw event stream for a window, optionally filtered to one ability. Used for
# avoidable-damage tracking (e.g. Glaive hits on Midnight Falls): unlike the
# aggregate tables, events carry per-hit `timestamp` and `targetID`, which we
# need to trim each pull to its "live" portion (before the Nth death). Paginated
# via `nextPageTimestamp`.
REPORT_EVENTS = """
query ReportEvents($code: String!, $startTime: Float!, $endTime: Float!, $fightIDs: [Int],
                   $dataType: EventDataType!, $abilityID: Float, $hostility: HostilityType) {
  reportData {
    report(code: $code) {
      events(startTime: $startTime, endTime: $endTime, fightIDs: $fightIDs,
             dataType: $dataType, abilityID: $abilityID, hostilityType: $hostility, limit: 10000) {
        data
        nextPageTimestamp
      }
    }
  }
}
"""

# Per-player role/spec breakdown for a window. `playerDetails` returns provider-
# shaped JSON bucketed into tanks/healers/dps, each entry carrying class (`type`)
# and a `specs` list of {spec, count} — count being fights played in that spec.
# This is WCL's own role classification; we use it to identify tanks (which the
# class-only damage table can't distinguish from same-class DPS).
REPORT_PLAYER_DETAILS = """
query ReportPlayerDetails($code: String!, $startTime: Float!, $endTime: Float!, $fightIDs: [Int]) {
  reportData {
    report(code: $code) {
      playerDetails(startTime: $startTime, endTime: $endTime, fightIDs: $fightIDs)
    }
  }
}
"""
