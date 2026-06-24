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

# Friendly buff *applications* for a set of fights, filtered server-side to the
# abilities we care about (defensives/consumables). The aggregate Buffs table is
# raid-wide only, so we attribute usage to players via these events. The
# filterExpression keeps the payload tiny (no pagination needed in practice).
REPORT_BUFF_EVENTS = """
query BuffEvents($code: String!, $startTime: Float!, $endTime: Float!,
                 $fightIDs: [Int], $filter: String!) {
  reportData {
    report(code: $code) {
      events(dataType: Buffs, hostilityType: Friendlies, startTime: $startTime,
             endTime: $endTime, fightIDs: $fightIDs, limit: 10000, filterExpression: $filter) {
        data
        nextPageTimestamp
      }
    }
  }
}
"""

# Aggregate Buffs table — used only to build a guid -> ability-name map for a report.
REPORT_BUFFS_TABLE = """
query BuffsTable($code: String!, $startTime: Float!, $endTime: Float!, $fightIDs: [Int]) {
  reportData {
    report(code: $code) {
      table(dataType: Buffs, startTime: $startTime, endTime: $endTime, fightIDs: $fightIDs)
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
