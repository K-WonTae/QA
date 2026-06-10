# 학사 공통코드 인덱스

## 포함 파일

- [학기구분(CZ01)](./학기구분(CZ01).md)
- [대학구분(CZ02)](./대학구분(CZ02).md)
- [학사일정구분(CZ04)](./학사일정구분(CZ04).md)
- [캠퍼스구분(CZ12)](./캠퍼스구분(CZ12).md)
- [주야구분(CZ14)](./주야구분(CZ14).md)
- [계열구분(CZ21)](./계열구분(CZ21).md)

## 조회 SQL

```sql
SELECT *
FROM BaseCode
WHERE GroupCode IN (
    'CZ01', -- 학기구분
    'CZ02', -- 대학구분
    'CZ04', -- 학사일정구분
    'CZ12', -- 캠퍼스구분
    'CZ14', -- 주야구분
    'CZ21'  -- 계열구분
)
ORDER BY GroupCode, SubCode;
```
