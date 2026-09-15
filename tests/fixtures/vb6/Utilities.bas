Attribute VB_Name = "Utilities"
Option Explicit

Public Const DefaultName As String = "New customer"
Public Type CustomerRecord
    Name As String
    Count As Long
End Type

Public Enum CustomerStatus
    Active = 1
    Inactive = 2
End Enum

Public Declare Sub Sleep Lib "kernel32" (ByVal milliseconds As Long)

Public Sub Main()
    Prepare
    Call Prepare
    Dim count As Long
    count = DoubleCount(2)
    Sleep 1
End Sub

Private Sub Prepare()
    ' Ignore DoubleCount(9) in comments.
    Debug.Print "Prepare: DoubleCount(9)"
End Sub

Public Function DoubleCount(ByVal value As Long) As Long
    DoubleCount = value * 2
End Function
