using './main.bicep'

param location = readEnvironmentVariable('FOUNDRY_LOCATION')
param resourcePrefix = 'contoso-agents'
param projectNames = [
  'travel'
  'support'
  'research'
  'platform'
]
param githubEnvironment = 'contoso-agents'
param githubOidcSubject = readEnvironmentVariable('AZURE_GITHUB_OIDC_SUBJECT')
param budgetContactEmails = [
  readEnvironmentVariable('BUDGET_CONTACT_EMAIL')
]
param deploySreAgent = false
param deployApprovalsWorkflow = false
param monthlyBudgetUsd = 500
param tags = {
  project: 'contoso-agents'
  'managed-by': 'microsoft-foundry-demo'
  boundary: 'rg-contoso-agents'
  'cost-centre': 'demo'
}