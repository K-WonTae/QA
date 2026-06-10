# 학사 테이블 정리

> `D:\DeskList\업무\학사`(2021~2026 업무 SQL/TXT 516개)에서 사용되는 학사 시스템(MS-SQL) DB 테이블을 도메인별로 정리.
> 컬럼–공통코드 매핑은 [knowledge/학사](../index.md)의 코드 문서와 상호 링크.

## 도메인 문서
- [학적](학적.md) — `SHJ_` 학적 마스터·학적변동·상벌·교환·소속변동
- [입학·입시](입학·입시.md) — `SIS_` 입학원서·입학등록금·입학장학·학번부여
- [장학](장학.md) — `SJH_` 장학 마스터·장학코드 마스터·학자금대출
- [등록·수납](등록·수납.md) — `SDL_` 등록금 마스터·수납 헤더·분납·등록금액 기준
- [수업·시간표](수업·시간표.md) — `SSE_` 개설강좌·시간표·담당교수·수업계획서
- [수강·강의평가](수강·강의평가.md) — `SSG_` 수강신청·출석부·강의평가
- [성적·졸업](성적·졸업.md) — `SSJ_` 성적·누계 / `SJE_` 졸업·졸업사정
- [조직·기타](조직·기타.md) — `COR_` 소속·계열·학사일정 / `SGG_` 교과과정 / 기타 기준

## 테이블 접두어 ↔ 도메인
| 접두어 | 도메인 | 핵심 테이블 |
|---|---|---|
| SHJ_ | 학적 | SHJ_Hagjeog_M |
| SIS_ | 입학·입시 | SIS_IbhagDeungloggeum_M, SIS_IbhagJanghag_M |
| SJH_ | 장학 | SJH_Janghag_M, SJH_JanghagCd_M |
| SDL_ | 등록·수납 | SDL_Deunglog_M, SDL_Sunab_H |
| SSE_ | 수업·시간표 | SSE_GaeseolGangjwa_M |
| SSG_ | 수강·강의평가 | SSG_SugangSincheong_M, SSG_GanguiPyeongga* |
| SSJ_ | 성적 | SSJ_Seongjeog_M |
| SJE_ | 졸업 | SJE_Joleob_M, SJE_JoleobSajeongJaryo_H |
| COR_ | 조직·공통기준 | COR_SosogCd_M, COR_SosogGyeyeol_M |

## 공통코드 매핑 규칙
SQL의 코드 리터럴은 공통코드 마스터(`BaseCode`)의 `SubCode`와 동일하다.
- 학사계열: 리터럴 앞에 `S` → `I1601`(SunabGb) = **SI16** 수납구분 `완납`
- 공통계열: 리터럴 앞에 `C` → `Z0201`(DaehagGb) = **CZ02** 대학구분
- 명칭 조회: `SF_COR_GongtongCdNm_Johoe(구분, 코드)`

자주 쓰는 컬럼 ↔ 코드:
| 컬럼 | 코드값 예 | 공통코드 |
|---|---|---|
| DaehagGb | Z0201/Z0202 | [대학구분 CZ02](../학사/대학구분(CZ02).md) |
| Haggi | Z0101/Z0102 | [학기구분 CZ01](../학사/학기구분(CZ01).md) |
| GyeyeolGb | Z2101 | [계열구분 CZ21](../학사/계열구분(CZ21).md) |
| JuyaGb | Z1401 | [주야구분 CZ14](../학사/주야구분(CZ14).md) |
| IbhagGb(입학) | A3101/A3102 | [입학구분 SA31](../입학/입학구분(SA31).md) |
| MojibGb | A0102 | [모집구분 SA01](../입학/모집구분(SA01).md) |
| IbhagGb/HjbdGb(학적) | B3301/B3704 | [학적변동구분 SB37](../학적/학적변동구분(SB37).md) |
| GwajeongGb | B2601/B2607 | [과정구분 SB26](../학적/과정구분(SB26).md) |
| HagjeogStGb | B0301/B0308 | [학적상태 SB03](../학적/학적상태(SB03).md) |
| IsuGb | D0201/D0212 | [이수구분 SD02](../수업/이수구분(SD02).md) |
| GangjwaGb | C0401 | [강좌구분 SC04](../수업/강좌구분(SC04).md) |
| Deunggeub | A+/F/P | [성적구분 SF01](../수업/성적구분(SF01).md) |
| SunabGb | I1601~I1606 | [수납구분 SI16](../등록/수납구분(SI16).md) |
| SunabHwanbulGb | I1701/I1702 | [수납환불구분 SI17](../등록/수납환불구분(SI17).md) |
| EunhaengCd/BankGb | I0801 | [등록은행 SI08](../등록/등록은행(SI08).md) |
| Chasu/JanghagChasu | H0801 | [차수 SH08](../장학/차수(SH08).md) |
| SuhyeGb | H0701/H0702 | [수혜구분 SH07](../장학/수혜구분(SH07).md) |
| JanghagGb | H0101/H0103 | [장학구분 SH01](../장학/장학구분(SH01).md) |

## knowledge 미등재 코드 (추가 검토)
- `A53` 예수납구분 (`A5301` 예치/`A5302`) — SIS_IbhagDeungloggeum_M
- `I15` 수납여부 (`I1501`) — SDL_Bunnab_H.SunabYn

## 비고
- 한글 주석은 원본 파일이 CP949 인코딩이라 일부 도구에서 깨져 보일 수 있음(코드/컬럼명은 영문이라 무관).
- 컬럼 목록은 SQL 사용처(INSERT…SELECT 별칭, WHERE 조건, SP 파라미터) 기반 관찰값으로, DDL 전수는 아님. "(추정)" 표기는 검증 필요 항목.
