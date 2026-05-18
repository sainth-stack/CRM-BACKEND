import pandas as pd

class ProspectService:
    def filter_top_prospects(self, prospects: list, limit: int = 4):
        """
        Filters and ranks prospects for a specific company.
        Ranking criteria:
        1. Email Validation (Valid > Unknown > Invalid)
        2. Seniority (C-Suite > VP > Director > Manager)
        3. Tenure (Not implemented in CSV yet, but ready for extension)
        """
        if not prospects:
            return []

        df = pd.DataFrame(prospects)
        
        # 1. Seniority Score
        seniority_map = {
            'Owner': 10,
            'Founder': 10,
            'CEO': 10,
            'C-Suite': 9,
            'Partner': 8,
            'VP': 7,
            'Vice President': 7,
            'Director': 5,
            'Manager': 3,
            'Senior': 2,
            'Junior': 1
        }
        
        def calculate_seniority_score(row):
            title = str(row.get('role', '')).lower()
            seniority_val = str(row.get('seniority', '')).lower()
            
            score = 0
            for keyword, s_score in seniority_map.items():
                if keyword.lower() in title or keyword.lower() in seniority_val:
                    score = max(score, s_score)
            return score

        df['seniority_score'] = df.apply(calculate_seniority_score, axis=1)

        # 2. Email Validation Score
        email_map = {
            'Valid': 10,
            'Accept All': 5,
            'Unknown': 2,
            'Invalid': 0
        }
        df['email_score'] = df['email_status'].apply(lambda x: email_map.get(x, 0))

        # 3. Combined Score
        df['total_score'] = df['seniority_score'] * 2 + df['email_score']

        # Sort and take top N
        df = df.sort_values(by='total_score', ascending=False)
        top_prospects = df.head(limit).to_dict('records')

        return top_prospects
