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
param apiManagementPublisherEmail = readEnvironmentVariable('APIM_PUBLISHER_EMAIL')
param existingModelDeployments = json(readEnvironmentVariable('EXISTING_MODEL_DEPLOYMENTS_JSON'))
param deploySreAgent = false
param deployApprovalsWorkflow = false
param monthlyBudgetUsd = 500
param budgetStartDate = readEnvironmentVariable('BUDGET_START_DATE')
param tags = {
  project: 'contoso-agents'
  'managed-by': 'microsoft-foundry-demo'
  boundary: 'rg-contoso-agents'
  'cost-centre': 'demo'
}