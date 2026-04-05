# Azure Container Apps: Easy Auth & Entra ID Configuration

This guide summarizes the setup and core concepts for implementing built-in authentication (Easy Auth) for Azure Container Apps using Microsoft Entra ID.

---

## 1. Steps to Enable Easy Auth for a New Application

Follow these steps to set up a new Container App with its own dedicated App Registration.

### **Phase A: Create the App Registration**
1.  In the **Azure Portal**, search for **Microsoft Entra ID** > **App registrations** > **+ New registration**.
2.  **Name**: Enter a name (e.g., `my-app-auth`).
3.  **Account Types**: Select "Accounts in this organizational directory only" (Single Tenant).
4.  **Redirect URI**: Leave blank for now; click **Register**.
5.  **Copy Details**: Save the **Application (client) ID** and **Directory (tenant) ID** for later.

### **Phase B: Configure Platform Settings**
1.  In the new registration, go to **Authentication** > **+ Add a platform** > **Web**.
2.  **Redirect URI**: Enter your app's callback URL: 
    `https://<your-app-name>.<region>.azurecontainerapps.io/.auth/login/aad/callback`.
3.  **Implicit grant**: Check the box for **ID tokens**.
4.  Click **Configure**.

### **Phase C: Enable Easy Auth on Container App**
1.  Go to your **Container App** > **Security** > **Authentication** > **Add identity provider**.
2.  **Identity Provider**: Select **Microsoft**.
3.  **App registration type**: Select "Provide the details of an existing app registration".
4.  **Details**: Paste the **Client ID** and **Tenant ID** from Phase A.
5.  **Unauthenticated requests**: Set to **HTTP 302 Found redirect** (for websites).
6.  Click **Add**.

---

## 2. Authentication Flow
1.  **Initial Request**: A user navigates to your Container App URL.
2.  **Middleware Check**: The **Easy Auth sidecar** intercepts the request.
3.  **Redirect (302)**: Since the user is unauthenticated, the sidecar redirects the browser to **Microsoft Entra ID**.
4.  **User Login**: The user enters their credentials.
5.  **Token Issuance**: Entra ID sends an **ID Token** back to the app's callback endpoint.
6.  **Session Created**: Easy Auth validates the token and sets an auth cookie in the browser.

---

## 3. Platform Endpoints

### `/.auth/login/aad/callback`
*   **The Landing Strip**: The internal path where Entra ID sends the user after a successful login.
*   **Requirement**: This **exact URL** must match the **Redirect URI** in your Entra ID App Registration.

### `/.auth/me`
*   **The Identity Mirror**: A built-in JSON endpoint that returns the currently logged-in user's claims (name, email, roles).

---

## 4. ID Token vs. Access Token


| Feature | **ID Token** (`id_token`) | **Access Token** (`access_token`) |
| :--- | :--- | :--- |
| **Purpose** | **Identity**: Proves *who* the user is. | **Authorization**: Proves *what* they can do. |
| **Recipient** | Your **Application** (the client). | A **Resource API** (like Microsoft Graph). |
| **Easy Auth Role** | **Required**. Used to sign the user in. | **Optional**. Used to call external APIs. |

---

## 5. Multi-App Management
If you have multiple Container Apps using the same App Registration:
*   **Append, don't replace**: Add each app's unique callback URL to the **Redirect URIs** list in the **Authentication** blade.
*   **Limit**: Entra ID supports up to 256 URIs per registration.

---

## 6. Security Hardening (Restricting Access)
To prevent all users in your company from accessing the app:
1.  Go to **Enterprise Applications** > Search for your App Name.
2.  **Properties**: Set **Assignment Required?** to **Yes**.
3.  **Users and groups**: Assign only the specific people or groups allowed to use the app.