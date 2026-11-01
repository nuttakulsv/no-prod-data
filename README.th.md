# no-prod-data

[English](README.md) · จากคนทำ [safe-migrations](https://github.com/nuttakulsv/safe-migrations) และ [thai-pii-scan](https://github.com/nuttakulsv/thai-pii-scan)

ถาม agent ว่าทำไมออเดอร์ค้าง มันไปเปิด `.env` เจอ `DATABASE_URL` แล้วรัน

```
psql "$DATABASE_URL" -c "select * from orders o join customers c on c.id = o.customer_id order by o.created_at desc limit 50"
```

ปรากฏว่า `DATABASE_URL` ชี้ไปที่ production ชื่อ อีเมล เบอร์โทร และที่อยู่ของลูกค้า 50 คนก็เข้ามาอยู่ในบทสนทนา และถูกส่งต่อไปที่ผู้ให้บริการโมเดลแล้ว ไม่มีอะไรถูกลบหรือพัง แต่กลายเป็นปัญหาเรื่องการคุ้มครองข้อมูลส่วนบุคคล (PDPA) ไปแล้ว

no-prod-data เป็น hook ของ Claude Code ที่จะถามก่อนทุกครั้งที่คำสั่งจะดึงแถวข้อมูลออกมาจากฐานข้อมูลหรือที่เก็บข้อมูลของ production ส่วนการดูโครงสร้างตารางและการนับจำนวนให้ผ่านไปได้ เพราะส่วนใหญ่ agent ก็ต้องการแค่นั้น

## ถามเมื่อไหร่

**คำสั่งที่อ่านข้อมูล** เช่น `psql`, `mysql`/`mariadb`, `mongosh`, `redis-cli` ที่อ่านค่า (`GET`, `HGETALL`, `KEYS`...), `sqlcmd`, `bq query`, การ dump ข้อมูล (`pg_dump`, `mysqldump`, `mongodump`, `mongoexport`, `supabase db dump`), `heroku pg:psql`, `aws dynamodb scan/query`, `aws s3 cp s3://...` และ `kubectl exec` / `docker exec` เข้าไปใน client ของฐานข้อมูล รวมถึงการส่งคำสั่งผ่าน pipe (`echo "select..." | psql`)

**...เมื่อชี้ไปที่ production** เช่น มีคำว่า `prod`/`production` ในชื่อ host, context, profile หรือชื่อแอป, เป็น host ของฐานข้อมูลบน cloud (`*.rds.amazonaws.com`, `*.supabase.co`, `*.neon.tech`, `*.psdb.cloud`, `*.mongodb.net` และอื่นๆ) หรือใช้ `supabase --linked` นอกจากนี้ยังไปดูค่าของตัวแปรอย่าง `$DATABASE_URL` ใน environment และไฟล์ `.env` ของโปรเจกต์ด้วย `psql $DATABASE_URL` จึงโดนจับถ้า URL นั้นเป็น production (โดยไม่แสดงค่าออกมา)

**เครื่องมือฐานข้อมูลผ่าน MCP** ก็ด้วย ถ้าชื่อ server บอกว่าเป็น production เช่น `mcp__postgres-prod__query`

**ปล่อยผ่าน** `\d`, `\dt`, query ใน `information_schema` และ `pg_catalog`, `SHOW`, `DESCRIBE`, `EXPLAIN` (แต่ไม่ใช่ `EXPLAIN ANALYZE` เพราะมันรัน query จริง), `select count(*)`, dump แบบ `--schema-only` / `--no-data`, `redis-cli INFO`/`PING`, `countDocuments()` และทุกอย่างที่ไม่ได้ชี้ไปที่ production

เป็นการ "ถาม" ไม่ใช่ "ห้าม" เพราะบางครั้งการอ่านข้อมูล production สักแถวก็จำเป็นจริง ให้เราเป็นคนตัดสินใจ

## ติดตั้ง

Claude Code (hook และ skill)

```
/plugin marketplace add nuttakulsv/no-prod-data
/plugin install no-prod-data@no-prod-data
```

ใช้แค่ skill กับ Codex, Cursor และตัวอื่นๆ ก็ได้ skill จะสอนวิธี debug production โดยไม่ต้องดึงข้อมูลลูกค้า ได้แก่ ดูโครงสร้างก่อน, ใช้ตัวเลขสรุปแทนการดึงแถว, ถ้าจำเป็นให้ดึงทีละแถวด้วย id เฉพาะคอลัมน์ที่ต้องใช้ และห้ามเอาข้อมูล production ไปใส่ใน fixture หรือคำตอบ

```
npx skills add nuttakulsv/no-prod-data
```

ลองเช็คคำสั่งเองได้

```
python3 hooks/prodread.py --check 'psql "$DATABASE_URL" -c "select * from users limit 5"'
```

## ข้อจำกัด

- รู้ว่าเป็น production จากชื่อและ host ถ้าฐานข้อมูล production ชื่อ `db1` อยู่บน IP ภายใน ก็ดูไม่ต่างจากตัวอื่น ควรตั้งชื่อ host, context และ profile ให้มีคำว่า prod
- ดูได้แค่ command line ถ้า agent เขียนสคริปต์ที่ query production เอง (`python debug.py`) จะหลุด
- hook เป็นแค่ชั้นหนึ่ง ชั้นที่แข็งแรงกว่าคือ role แบบอ่านอย่างเดียว, replica ที่ปิดบังคอลัมน์ข้อมูลส่วนบุคคล และไม่ให้ credential ของ production อยู่ใน environment ของ agent เลย
- การเขียนหรือลบข้อมูลเป็นอีกเรื่อง ดู [safe-migrations](https://github.com/nuttakulsv/safe-migrations)

## รันเทสต์

```
python3 hooks/prodread.py --test
```

## License

MIT
