cd ~/Developer/MetaHackaton/vision-plc-agent

PI=https://192.168.1.198:8443
PW=Stan1234.

code=$(curl -sk -o /tmp/create-user.out -w '%{http_code}' -X POST "$PI/api/create-user" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$PW\"}")
echo "create-user -> HTTP $code: $(cat /tmp/create-user.out)"; rm -f /tmp/create-user.out

if [ "$code" = "201" ]; then
  [ -f .env ] || cp .env.example .env
  sed -i -e "s|^OPENPLC_URL=.*|OPENPLC_URL=$PI|" \
         -e "s|^OPENPLC_USER=.*|OPENPLC_USER=admin|" \
         -e "s|^OPENPLC_PASSWORD=.*|OPENPLC_PASSWORD=$PW|" .env
  chmod 600 .env
  echo "OK: credentials saved in $(pwd)/.env"
else
  echo "NOT created, .env untouched (401 would mean a user already exists)"
fi
unset PW

To verify the login afterwards, this prints only the HTTP code and whether a token came back:

U=$(grep '^OPENPLC_USER=' .env | cut -d= -f2-); P=$(grep '^OPENPLC_PASSWORD=' .env | cut -d= -f2-)
curl -sk -X POST https://192.168.1.198:8443/api/login -H 'Content-Type: application/json' \
  -d "{\"username\":\"$U\",\"password\":\"$P\"}" -w '\nHTTP %{http_code}\n' | sed -E 's/("access_token":")[^"]{8}[^"]*/\1<redacted>/'
unset U P
