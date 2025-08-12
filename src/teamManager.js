class TeamManager {
  constructor() {
    // 팀 구성원 정보 (실제 조직도 반영)
    this.teams = {
      'SNS 1팀': [
        '이종민', '정주연', '이혜영', '김국현', 
        '정다혜', '조시현', '김시윤'
      ],
      'SNS 2팀': [
        '윤도우리', '신혜서', '김상아', '박은진', 
        '오민환', '서정국'
      ],
      'SNS 3팀': [
        '김진후', '김시진', '권재현', '김지원', 
        '최호익', '김진협', '박해영'
      ],
      'SNS 4팀': [
        '이민주', '전지윤', '전미란', '김채영', 
        '김영진', '강헌준'
      ],
      '의정부 SNS팀': [
        '차정환', '최수능', '구본영', '서민국', 
        '오민경', '김범주', '동수진', '성일훈'
      ]
    };
    
    // 역방향 맵핑 생성 (이름 -> 팀)
    this.memberToTeam = {};
    for (const [team, members] of Object.entries(this.teams)) {
      for (const member of members) {
        this.memberToTeam[member] = team;
      }
    }
    
    // 별칭 매핑 (채널톡 이름과 실제 이름이 다른 경우)
    this.aliases = {
      // 예시: '채널톡표시이름': '실제이름'
      // '김OO': '김국현',
    };
  }

  getTeamByName(name) {
    if (!name || name === '미배정') return '없음';
    
    // 정확한 매칭
    if (this.memberToTeam[name]) {
      return this.memberToTeam[name];
    }
    
    // 별칭 확인
    if (this.aliases[name]) {
      const realName = this.aliases[name];
      if (this.memberToTeam[realName]) {
        return this.memberToTeam[realName];
      }
    }
    
    // 부분 매칭 시도 (성만 같은 경우 등)
    for (const [member, team] of Object.entries(this.memberToTeam)) {
      // 성이 같고 이름 길이가 같은 경우
      if (name.length === member.length && name[0] === member[0]) {
        // 더 정확한 매칭을 위해 추가 검증 가능
        console.log(`Partial match: ${name} → ${member} (${team})`);
        return team;
      }
    }
    
    // 이메일에서 이름 추출 시도
    const emailMatch = name.match(/^([^@]+)@/);
    if (emailMatch) {
      const emailName = emailMatch[1];
      
      // 이메일 ID로 다시 검색
      if (this.memberToTeam[emailName]) {
        return this.memberToTeam[emailName];
      }
      
      // 이메일 ID가 영문인 경우 처리
      // 예: jongmin.lee@company.com → 이종민
      const koreanName = this.findKoreanNameByEmail(emailName);
      if (koreanName && this.memberToTeam[koreanName]) {
        return this.memberToTeam[koreanName];
      }
    }
    
    console.log(`Team not found for: ${name}`);
    return '없음';
  }

  findKoreanNameByEmail(emailId) {
    // 영문 이메일 ID를 한글 이름으로 매핑
    const emailToName = {
      'jongmin': '이종민',
      'jongmin.lee': '이종민',
      'ljm': '이종민',
      'juyeon': '정주연',
      'jjy': '정주연',
      'hyeyoung': '이혜영',
      'lhy': '이혜영',
      'kukhyun': '김국현',
      'kgh': '김국현',
      'dahye': '정다혜',
      'jdh': '정다혜',
      'sihyun': '조시현',
      'csh': '조시현',
      'siyoon': '김시윤',
      'ksy': '김시윤',
      // SNS 2팀
      'dowoo': '윤도우리',
      'ydu': '윤도우리',
      'hyeseo': '신혜서',
      'shs': '신혜서',
      'sangah': '김상아',
      'ksa': '김상아',
      'eunjin': '박은진',
      'pej': '박은진',
      'minhwan': '오민환',
      'omh': '오민환',
      'jungkook': '서정국',
      'sjk': '서정국',
      // SNS 3팀
      'jinhoo': '김진후',
      'kjh': '김진후',
      'sijin': '김시진',
      'ksj': '김시진',
      'jaehyun': '권재현',
      'kjh2': '권재현',
      'jiwon': '김지원',
      'kjw': '김지원',
      'hoik': '최호익',
      'chi': '최호익',
      'jinhyup': '김진협',
      'kjh3': '김진협',
      'haeyoung': '박해영',
      'phy': '박해영',
      // SNS 4팀
      'minju': '이민주',
      'lmj': '이민주',
      'jiyoon': '전지윤',
      'jjy2': '전지윤',
      'miran': '전미란',
      'jmr': '전미란',
      'chaeyoung': '김채영',
      'kcy': '김채영',
      'youngjin': '김영진',
      'kyj': '김영진',
      'heonjun': '강헌준',
      'khj': '강헌준',
      // 의정부 SNS팀
      'junghwan': '차정환',
      'cjh': '차정환',
      'suneung': '최수능',
      'csn': '최수능',
      'bonyoung': '구본영',
      'kby': '구본영',
      'minkook': '서민국',
      'smk': '서민국',
      'minkyung': '오민경',
      'omk': '오민경',
      'bumju': '김범주',
      'kbj': '김범주',
      'sujin': '동수진',
      'dsj': '동수진',
      'ilhoon': '성일훈',
      'sih': '성일훈'
    };
    
    const lowerEmailId = emailId.toLowerCase();
    return emailToName[lowerEmailId] || null;
  }

  getAllTeams() {
    return Object.keys(this.teams);
  }

  getTeamMembers(teamName) {
    return this.teams[teamName] || [];
  }

  // 팀별 통계 반환
  getTeamStats(consultations) {
    const stats = {};
    
    // 초기화
    this.getAllTeams().forEach(team => {
      stats[team] = {
        total: 0,
        critical: 0,  // 10분 이상
        high: 0,      // 7-10분
        medium: 0,    // 4-7분
        low: 0        // 4분 미만
      };
    });
    
    stats['없음'] = {
      total: 0,
      critical: 0,
      high: 0,
      medium: 0,
      low: 0
    };
    
    // 통계 계산
    consultations.forEach(c => {
      const team = c.team || '없음';
      const waitTime = parseInt(c.waitTime || 0);
      
      if (stats[team]) {
        stats[team].total++;
        
        if (waitTime >= 10) stats[team].critical++;
        else if (waitTime >= 7) stats[team].high++;
        else if (waitTime >= 4) stats[team].medium++;
        else stats[team].low++;
      }
    });
    
    return stats;
  }
}

module.exports = TeamManager;
