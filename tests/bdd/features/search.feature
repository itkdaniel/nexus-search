Feature: Full-text project search
  As a developer using the nexus-search API
  I want to search for projects using natural language queries
  So that I can quickly find relevant projects

  Background:
    Given the nexus-search service is running with sample projects

  Scenario: BM25 search returns relevant results
    When I search for "authentication"
    Then the response should include the "Auth Service" project
    And the response status should be 200

  Scenario: Fuzzy fallback when BM25 returns no results
    When I search for "authetication"
    Then I should receive some results
    And the response status should be 200

  Scenario: Tag filter narrows search results
    When I search for "service" with tag filter "auth"
    Then all returned projects should have the "auth" tag

  Scenario: Empty result when no match
    When I search for "zzznomatchxxx"
    Then I should receive zero or more results
    And the response status should be 200
