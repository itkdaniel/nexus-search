Feature: Tag-graph BFS recommendations
  As a developer browsing the portfolio
  I want to see related projects when viewing a project
  So that I can discover similar work

  Background:
    Given the nexus-search service is running with sample projects

  Scenario: Related projects found via shared tags
    When I request related projects for the "Auth Service"
    Then I should receive at least one related project
    And all related projects should share at least one tag with "Auth Service"

  Scenario: Max results parameter is respected
    When I request 1 related projects for the "Auth Service"
    Then I should receive at most 1 result

  Scenario: Unknown project returns 404
    When I request related projects for a non-existent project
    Then the response status should be 404
