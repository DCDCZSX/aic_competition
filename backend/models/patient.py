from dataclasses import dataclass

@dataclass
class Patient:
    name: str
    age: int
    gender: str
    medical_history: str
    main_issue: str
    
    def to_dict(self):
        return {
            'name': self.name,
            'age': self.age,
            'gender': self.gender,
            'medical_history': self.medical_history,
            'main_issue': self.main_issue
        }


