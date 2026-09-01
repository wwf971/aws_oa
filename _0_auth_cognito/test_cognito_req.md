


# Requirement for test_cofnigo sub-project

Althouth this sub-project is called test_cognito, the demonstration utilizes multiple aws services for building a minimum web application that has a user registration page, and a user login page.

Admin


## User Model

There are two types of users: admin, and guest. There should be 






## Architecture Ensurement

The should be a python script that supports accessing the aws and ensure the designed architecture is present contact is aws, including existent of all kinds of resource objects, as well as correct configuration on them.

The architecture ensurement on cognito service should follow the following steps:

```text
ensure user pool
       ↓
ensure app client
       ↓
disable public signup(in case self_signup_enabled==false)
       ↓
ensure groups
       ↓
ensure users
       ↓
ensure group memberships
```

There should be a dynamodb table that maintains a user table as source of truth. Each user has an unique user id, and can be mapped to different identities from different auth system. Each mapping could be conceptually as a tupple. (userId, userIdFromAuthService, AuthSystemType, ...). For example, if user is from Cognito, then (userId, sub, 'Cognito', ...). The key and index settings should allow fast retrival in both direction: 1. given userId, know all identities in auth services quickly. 2. given userIdFromAuthService, know userId quickly.

For user created from script using AdminXxx apis, there should be a special row. with userIdFromAuthService being null, and AuthSytemType being a special value.
One user can have mappings to multiple identities from multiple auth services. Thus one userId might have multiple rows in the table.





Typical Login Process

```text
app.example.com
    │
    │ redirect
    ▼
auth.example.com/oauth2/authorize
    │
    ▼
Cognito login
    │
    │ redirect_uri
    ▼
app.example.com/callback
```


```
Cognito User Pool
├─ Users
├─ Groups
├─ App Client
│  └─ Frontend uses it for OAuth/OIDC login
└─ Domain
   └─ provide endpoints such as /authorize /token /logout

API Gateway
├─ JWT Authorizer
│  ├─ issuer   → Cognito User Pool
│  └─ audience → App Client
└─ Routes
   └─ /api/* → Lambda
```


## Architecture Design

```
Cognito User Pool
       │
       │ authenticate
       ▼
      JWT
{
  sub,
  email,
  cognito:groups: ["appA", "appB"]
}
       │
       ▼
   API Gateway
       │
       ▼
     Lambda
       │
       ├── know user identity(sub --> userId)
       └── know what user can do based on group
```


## API Configuration

API structure.

```
Public URLs
├─ app.example.com
│  └─ CloudFront Distribution
│     ├─ /*        → S3
│     │              └─ HTML / JS / CSS
│     │
│     └─ /api/*    → API Gateway
│                    └─ Route → Lambda
│
└─ auth.example.com
   └─ Cognito User Pool Domain
      ├─ /oauth2/authorize
      ├─ /oauth2/token
      ├─ /login
      └─ /logout
```


API call process.

```
Browser
├─ https://app.example.com/*
│      → CloudFront → S3
│
├─ https://auth.example.com/oauth2/authorize
│      → Cognito Managed Login
│
└─ https://app.example.com/api/*
       → CloudFront → API Gateway → Lambda
```
